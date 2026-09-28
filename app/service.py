"""核心业务：片段接收、水位线推进、永久缺口裁决、追加式更正与发布快照。

不变式（由单事务保证）：
- finalized 与 gaps 中每个频道的序号恰好是 1..watermark 的连续前缀，
  每个序号要么是定稿字幕，要么是永久缺口标记；无缺口的片段只存在于
  fragments 缓冲区；
- 同一序号只保留最高修订号，定稿后不再接受任何修改；
- 缺口裁决区间起点必须等于当前水位线下一位，且区间内不得存在已缓冲
  片段，校验失败不产生任何写入；
- 重复提交已完整裁决的同一区间幂等返回当前结果，部分重叠一律拒绝。

更正层（corrections）不变式：
- 只允许更正 finalized 中的定稿字幕；缺口序号与未定稿序号一律拒绝且不写入；
- corrections 表只追加：version 在每个 (channel, seq) 内从 1 起单调递增，
  UPDATE/DELETE 被触发器禁止，原始定稿文本永远不被改写；
- expected_version 必须等于当前更正版本（无更正时为 0），否则返回
  version_conflict；“读取当前版本 -> 校验 -> 追加下一版本”在同一个
  BEGIN IMMEDIATE 事务内完成，竞争请求只有一个能成功。

批量更正（correction_batches/correction_batch_items）不变式：
- 每批 1~100 项，同一批内 seq 必须互不相同，重复 seq 整批拒绝（409
  batch_conflict，冲突明细按输入顺序返回）且零写入；
- “逐项核对（已定稿字幕、非缺口、expected_version 等于当前版本）->
  按输入顺序追加各序号的新更正版本 -> 记录批次与逐项冻结结果”全部在
  单个 BEGIN IMMEDIATE 事务内完成；任一项校验失败即整批回滚，
  corrections 表不增加任何版本；
- (channel, idempotency_key) 唯一：相同频道、相同幂等键且请求内容
  （逐项 seq/expected_version/text/reason 及输入顺序，经指纹比对）
  完全相同时重试，原样返回原批次结果（idempotent=true，同一 batch_id）；
  键相同而内容不同返回 409 idempotency_key_conflict；
- 幂等记录只追加：correction_batches / correction_batch_items 与
  corrections 一样在存储层禁止 UPDATE/DELETE。

发布快照（snapshots/snapshot_items/snapshot_corrections）不变式：
- “锁定当前 watermark -> 固化 1..watermark 的字幕 / 缺口及各条更正
  历史 -> 写入快照”在单个 BEGIN IMMEDIATE 事务内完成，冻结结果与
  创建时刻的定稿状态严格一致；
- 快照完全追加且不可变：同一频道的同一 idempotency_key 重试不重新
  冻结，始终返回原 snapshot_id；此后新定稿、新更正、新缺口只写入
  原表，任何既有快照内容都不随之改变；
- 不存在的频道也可创建 watermark=0 的空快照（不创建频道行）。

快照差异（diff_snapshots）不变式：
- 只允许比较同一频道的两个既有快照；任一快照不存在 -> 404，
  频道不一致 -> 409，校验失败不产生任何写入；
- 差异基于快照冻结的生效内容：字幕以 effective_text 与
  correction_version 判定，缺口以裁决区间端点 (start_seq, end_seq)
  判定，两侧完全一致（含 kind 相同且内容相同）的序号不返回；
- 按 seq 升序合并两份快照条目，差异分 added（仅对方有）/ removed
  （仅基准有）/ changed（双方都有但内容不同）三类；after/limit 分页
  只计差异条目，next_after 指向本页最后一条差异的 seq，稀疏差异
  也不会因中间大量未变化条目而漏项。

快照敏感词遮盖（redact_snapshot）不变式：
- 遮盖是纯只读计算：只读取 snapshot_items 中冻结的生效文本，快照表与
  定稿 / 更正原表均不被修改；
- 匹配在整个快照 1..watermark 上进行：相邻字幕（中间无缺口）按序号无
  分隔拼接，缺口阻断拼接——跨字幕（含跨页）词语完整命中，跨缺口词语
  绝不命中；
- 匹配逐码点、区分大小写、不做归一化；所有词的重叠命中都参与，覆盖
  区间取并集，每个被覆盖码点替换为一个 "*"（遮盖后码点数不变），再按
  各字幕原码点长度拆回原序号；未命中文字与缺口标记原样保留；
- 分页在遮盖完成后进行：after/limit 只决定返回哪些条目，next_after 在
  还有后续条目时指向本页末序号，否则为 null。

剪辑字幕导出（export_snapshot_edit）不变式：
- 导出是纯只读计算：只读取 snapshot_items 中冻结的生效文本，快照表与
  定稿 / 更正原表均不被修改；
- timings 须恰好覆盖快照内全部字幕 seq：缺字幕序号、含重复序号或含
  快照中不存在的序号（缺口 / 超界）都返回 422，不产生任何输出；
- clips 按输入顺序首尾拼接为成片时间轴：每条字幕与每个 clip 取左闭
  右开交集，非空才生成 cue 并平移到成片累计时间；一条字幕横跨多个
  clip 时按交集拆分为多条 cue，重复 clip 在不同偏移处保留副本，
  时间重叠的字幕各自生成 cue 不合并；
- cue 按 (start_ms, clip_index, seq) 升序返回；format=vtt 时渲染为
  合法 WebVTT，正文转义 & < >。

快照字幕引用迁移（migrate_snapshot_quote）不变式：
- 迁移是纯只读计算：只读取两个快照的 snapshot_items（冻结生效文本），
  快照表与定稿 / 更正原表均不被修改；
- 仅允许同频道的两个既有快照：任一快照不存在 -> 404，频道不一致
  -> 409；
- 源引用区间 [start_seq, end_seq]（含两端）必须落在源快照内，且区间
  内不得有缺口、不得有空洞；起止码点偏移（左闭右开）必须落在对应
  字幕文本的码点边界内，且提取出的引用必须非空，否则 422
  invalid_quote_range；
- 引用可跨越相邻（同段、中间无缺口）字幕；中间字幕为空时仍属于
  同一连续段，引用照常跨段；
- 在目标快照的连续字幕文本（相邻字幕无分隔拼接、缺口阻断）中逐码点
  枚举引用的全部出现：恰好一次 -> found 并返回对应起止序号 / 偏移；
  零次 -> missing；多次 -> ambiguous（绝不任选一个位置）。

稿件校稿对齐（align_script）不变式：
- 对齐是纯只读计算：只读取 snapshot_items 中冻结的生效文本（字幕行），
  快照表与定稿 / 更正原表均不被修改；
- 输入 1~100 条有序非空稿件行，每行带 1~3 的本行最多合并字幕数；
  匹配代价为稿件行与字幕块拼接文本（相邻字幕无分隔拼接）之间的
  Unicode 码点编辑距离（Levenshtein）；
- 动态规划求稿件行与字幕的全局最小代价顺序对齐：每行可匹配连续
  1..max_subtitles 条同段字幕（中间不得有缺口 / 空洞，字幕不得跨块
  复用），也可跳过稿件行或字幕；跳过代价为相应文本的码点数；
- 总代价相同时依次按“跳过项更少 -> 合并字幕更少（Σ(k-1) 更小）->
  匹配序号字典序更小”择优；返回逐行起止序号 / 距离及未匹配稿件行
  下标与未匹配字幕序号。

命名消费者拉取游标（consumers）不变式：
- 每个 (channel, consumer) 的 cursor 只能经确认接口单调推进，且只能
  推进到当前租约批次 [lease_from, lease_to] 内的序号；
- 同一消费者同一时刻至多一个未过期租约：领取与确认都在 BEGIN IMMEDIATE
  事务内完成“校验 -> 写入”，并发下只有一个请求能改写租约 / 游标；
- 租约过期（lease_expires_at 到期）后同一未确认批次可被重新领取并签发
  新令牌，旧令牌随之失效；确认成功即清空租约，下一批从新游标后继续；
- 确认接口的所有校验（游标、令牌、过期、批次范围）先于任何写入，
  失败时不改写消费者状态。
"""

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from .db import read_tx, write_tx


class CorrectionError(Exception):
    """更正 / 审计请求在业务前置条件上被拒绝；HTTP 层映射为 409/404。"""

    def __init__(
        self,
        error: str,
        message: str,
        status_code: int = 409,
        extra: "dict | None" = None,
    ):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status_code = status_code
        # 附加字段（如批量更正的逐项 conflicts），原样并入错误响应体
        self.extra = extra


class SnapshotError(Exception):
    """快照分页 / 审计请求在业务前置条件上被拒绝；HTTP 层映射为 404/409。"""

    def __init__(self, error: str, message: str, status_code: int = 404):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status_code = status_code


class ConsumerError(Exception):
    """消费者领取 / 确认 / 查询在业务前置条件上被拒绝；HTTP 层映射为 409/404。"""

    def __init__(self, error: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status_code = status_code


def _now() -> float:
    """当前 epoch 秒；测试可替换以确定性控制租约过期。"""
    return time.time()


def _iso_utc(ts: float) -> str:
    """epoch 秒转 UTC ISO8601 字符串（与既有 created_at 风格一致）。"""
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _watermark_of(conn, channel: str) -> int:
    """读取频道当前水位线；频道不存在时为 0。"""
    row = conn.execute(
        "SELECT watermark FROM channels WHERE channel = ?", (channel,)
    ).fetchone()
    return row["watermark"] if row else 0


def submit_fragment(channel: str, seq: int, revision: int, text: str) -> dict:
    """提交一个字幕片段，返回是否生效、最新水位线及本次新定稿的片段。"""
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO channels (channel, watermark) VALUES (?, 0) "
            "ON CONFLICT(channel) DO NOTHING",
            (channel,),
        )
        watermark = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()["watermark"]

        applied = False
        reason = None
        if seq <= watermark:
            # 已定稿（或已裁决为缺口）的序号不可再修改
            reason = "already_finalized"
        else:
            row = conn.execute(
                "SELECT revision FROM fragments WHERE channel = ? AND seq = ?",
                (channel, seq),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO fragments (channel, seq, revision, text) "
                    "VALUES (?, ?, ?, ?)",
                    (channel, seq, revision, text),
                )
                applied = True
            elif revision > row["revision"]:
                conn.execute(
                    "UPDATE fragments SET revision = ?, text = ? "
                    "WHERE channel = ? AND seq = ?",
                    (revision, text, channel, seq),
                )
                applied = True
            else:
                reason = "stale_revision"

        newly = _consume_contiguous(conn, channel, watermark)
        new_watermark = newly[-1]["seq"] if newly else watermark

        return {
            "channel": channel,
            "seq": seq,
            "applied": applied,
            "reason": reason,
            "watermark": new_watermark,
            "finalized": newly,
        }


def _consume_contiguous(conn, channel: str, watermark: int) -> list:
    """从 watermark+1 起消费连续的缓冲片段：移入定稿表并更新水位线。

    遇到第一个缺失（缺口或未到达）即停止，保证不会越过缺口定稿；
    缺口补齐（片段到达或被裁决为永久缺口）的操作会在一次事务里
    释放整段连续片段。
    """
    newly = []
    seq = watermark + 1
    while True:
        row = conn.execute(
            "SELECT revision, text FROM fragments WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if row is None:
            break
        conn.execute(
            "INSERT INTO finalized (channel, seq, revision, text) VALUES (?, ?, ?, ?)",
            (channel, seq, row["revision"], row["text"]),
        )
        conn.execute(
            "DELETE FROM fragments WHERE channel = ? AND seq = ?", (channel, seq)
        )
        newly.append(
            {
                "kind": "subtitle",
                "seq": seq,
                "revision": row["revision"],
                "text": row["text"],
                "start_seq": None,
                "end_seq": None,
            }
        )
        seq += 1
    if newly:
        conn.execute(
            "UPDATE channels SET watermark = ? WHERE channel = ?",
            (newly[-1]["seq"], channel),
        )
    return newly


def decide_gap(channel: str, start_seq: int, end_seq: int) -> dict:
    """裁决 [start_seq, end_seq] 为永久缺口。

    起点必须等于当前水位线下一位，区间内不得存在已缓冲片段。
    成功后在同一事务中记录被跳过序号、推进水位线，并继续释放紧随
    其后的连续缓冲片段。重复提交已完整裁决的同一区间幂等返回；
    与既有裁决部分重叠则拒绝。任何校验失败都不产生写入。
    """
    with write_tx() as conn:
        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0

        if start_seq <= watermark:
            return _historical_gap_response(conn, channel, start_seq, end_seq, watermark)

        if start_seq != watermark + 1:
            return _rejected(channel, watermark, "start_not_at_watermark")

        buffered = conn.execute(
            "SELECT seq FROM fragments WHERE channel = ? AND seq BETWEEN ? AND ? "
            "ORDER BY seq LIMIT 1",
            (channel, start_seq, end_seq),
        ).fetchone()
        if buffered is not None:
            # 区间内已有缓冲片段，必须等片段定稿而不能裁决丢失
            return _rejected(channel, watermark, "buffered_fragments")

        # 校验通过：以下写入与水位线推进在同一事务内完成
        conn.execute(
            "INSERT INTO channels (channel, watermark) VALUES (?, ?) "
            "ON CONFLICT(channel) DO UPDATE SET watermark = excluded.watermark",
            (channel, end_seq),
        )
        conn.execute(
            "INSERT INTO gap_decisions (channel, start_seq, end_seq, released_until) "
            "VALUES (?, ?, ?, ?)",
            (channel, start_seq, end_seq, end_seq),
        )
        conn.executemany(
            "INSERT INTO gaps (channel, seq, start_seq, end_seq) VALUES (?, ?, ?, ?)",
            [(channel, seq, start_seq, end_seq) for seq in range(start_seq, end_seq + 1)],
        )

        released = _consume_contiguous(conn, channel, end_seq)
        if released:
            conn.execute(
                "UPDATE gap_decisions SET released_until = ? "
                "WHERE channel = ? AND start_seq = ? AND end_seq = ?",
                (released[-1]["seq"], channel, start_seq, end_seq),
            )
        new_watermark = released[-1]["seq"] if released else end_seq

        return {
            "channel": channel,
            "applied": True,
            "idempotent": False,
            "reason": None,
            "watermark": new_watermark,
            "skipped": {"start_seq": start_seq, "end_seq": end_seq},
            "finalized": released,
        }


def _rejected(channel: str, watermark: int, reason: str) -> dict:
    """构造拒绝响应：不执行任何写入。"""
    return {
        "channel": channel,
        "applied": False,
        "idempotent": False,
        "reason": reason,
        "watermark": watermark,
        "skipped": None,
        "finalized": [],
    }


def _historical_gap_response(
    conn, channel: str, start_seq: int, end_seq: int, watermark: int
) -> dict:
    """处理起点位于水位线之前的提交：完全相同的区间幂等返回，否则拒绝。"""
    decision = conn.execute(
        "SELECT end_seq, released_until FROM gap_decisions "
        "WHERE channel = ? AND start_seq = ? AND end_seq = ?",
        (channel, start_seq, end_seq),
    ).fetchone()
    if decision is not None:
        released = _fetch_finalized(
            conn, channel, after=end_seq, upto=decision["released_until"]
        )
        return {
            "channel": channel,
            "applied": True,
            "idempotent": True,
            "reason": None,
            "watermark": watermark,
            "skipped": {"start_seq": start_seq, "end_seq": end_seq},
            "finalized": released,
        }

    overlap = conn.execute(
        "SELECT 1 FROM gap_decisions "
        "WHERE channel = ? AND start_seq <= ? AND end_seq >= ? LIMIT 1",
        (channel, end_seq, start_seq),
    ).fetchone()
    reason = "gap_overlap" if overlap is not None else "already_finalized"
    return _rejected(channel, watermark, reason)


def _fetch_finalized(conn, channel: str, after: int, upto: int) -> list:
    """读取 (after, upto] 区间内随某次裁决/提交新定稿的字幕片段。"""
    rows = conn.execute(
        "SELECT seq, revision, text FROM finalized "
        "WHERE channel = ? AND seq > ? AND seq <= ? ORDER BY seq",
        (channel, after, upto),
    ).fetchall()
    return [
        {
            "kind": "subtitle",
            "seq": row["seq"],
            "revision": row["revision"],
            "text": row["text"],
            "start_seq": None,
            "end_seq": None,
        }
        for row in rows
    ]


def get_finalized(
    channel: str, after: int = 0, limit: int = 1000, view: str = "original"
) -> dict:
    """查询频道定稿结果与当前水位线，按序混合返回字幕与缺口标记。

    after 用于增量拉取（按序号过滤，字幕与缺口标记统一计数）。
    view=original（默认）只返回不可变的原始定稿；view=effective 合并
    每个序号的最新更正文本并附带 correction_version，原始表不被读取改写。
    """
    with read_tx() as conn:
        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0

        if view == "original":
            return {"channel": channel, "watermark": watermark,
                    "items": _fetch_finalized_items(conn, channel, after, limit)}

        # effective：左连接每个序号的最新更正版本，未更正者 version 为 0
        rows = conn.execute(
            """
            SELECT base.seq AS seq, base.kind AS kind, base.revision AS revision,
                   base.text AS original_text, base.start_seq AS start_seq,
                   base.end_seq AS end_seq,
                   COALESCE(c.version, 0) AS correction_version,
                   COALESCE(c.text, base.text) AS effective_text
            FROM (
                SELECT f.seq AS seq, 'subtitle' AS kind, f.revision AS revision,
                       f.text AS text, NULL AS start_seq, NULL AS end_seq, 0 AS ord
                FROM finalized f
                WHERE f.channel = ? AND f.seq > ?
                UNION ALL
                SELECT g.seq AS seq, 'gap' AS kind, NULL AS revision, NULL AS text,
                       g.start_seq AS start_seq, g.end_seq AS end_seq, 1 AS ord
                FROM gaps g
                WHERE g.channel = ? AND g.seq > ?
            ) base
            LEFT JOIN corrections c
                   ON c.channel = ? AND c.seq = base.seq
                  AND c.version = (
                      SELECT MAX(c2.version) FROM corrections c2
                      WHERE c2.channel = ? AND c2.seq = base.seq
                  )
            ORDER BY base.seq, base.ord
            LIMIT ?
            """,
            (channel, after, channel, after, channel, channel, limit),
        ).fetchall()

        items = []
        page_max_version = 0
        for row in rows:
            if row["kind"] == "gap":
                items.append(
                    {
                        "kind": "gap",
                        "seq": row["seq"],
                        "revision": None,
                        "text": None,
                        "start_seq": row["start_seq"],
                        "end_seq": row["end_seq"],
                        "correction_version": None,
                    }
                )
            else:
                cv = row["correction_version"]
                page_max_version = max(page_max_version, cv)
                items.append(
                    {
                        "kind": "subtitle",
                        "seq": row["seq"],
                        "revision": row["revision"],
                        "text": row["effective_text"],
                        "start_seq": None,
                        "end_seq": None,
                        "correction_version": cv,
                    }
                )
        return {
            "channel": channel,
            "watermark": watermark,
            "view": "effective",
            "correction_version": page_max_version,
            "items": items,
        }


def _fetch_finalized_items(conn, channel: str, after: int, limit: int) -> list:
    """读取按序混合的字幕与缺口标记；effective 仅在 original 视图之外复用。"""
    rows = conn.execute(
        """
        SELECT f.seq AS seq, f.revision AS revision, f.text AS text,
               NULL AS start_seq, NULL AS end_seq, 0 AS ord
        FROM finalized f
        WHERE f.channel = ? AND f.seq > ?
        UNION ALL
        SELECT g.seq AS seq, NULL AS revision, NULL AS text,
               g.start_seq AS start_seq, g.end_seq AS end_seq, 1 AS ord
        FROM gaps g
        WHERE g.channel = ? AND g.seq > ?
        ORDER BY seq, ord LIMIT ?
        """,
        (channel, after, channel, after, limit),
    ).fetchall()
    items = []
    for row in rows:
        if row["start_seq"] is None:
            items.append(
                {
                    "kind": "subtitle",
                    "seq": row["seq"],
                    "revision": row["revision"],
                    "text": row["text"],
                    "start_seq": None,
                    "end_seq": None,
                }
            )
        else:
            items.append(
                {
                    "kind": "gap",
                    "seq": row["seq"],
                    "revision": None,
                    "text": None,
                    "start_seq": row["start_seq"],
                    "end_seq": row["end_seq"],
                }
            )
    return items


def submit_correction(
    channel: str, seq: int, expected_version: int, text: str, reason: str
) -> dict:
    """对已定稿字幕原子追加一条更正。

    仅当 (channel, seq) 存在于 finalized（不是缺口、不是未定稿序号），
    且 expected_version 等于当前更正版本（无更正为 0）时才写入；
    任何前置条件不满足都先于写入返回，corrections 表与 finalized 表
    均保持不变。
    """
    with write_tx() as conn:
        finalized = conn.execute(
            "SELECT 1 FROM finalized WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if finalized is None:
            gap = conn.execute(
                "SELECT 1 FROM gaps WHERE channel = ? AND seq = ?",
                (channel, seq),
            ).fetchone()
            if gap is not None:
                raise CorrectionError(
                    "seq_is_gap",
                    f"序号 {seq} 已裁决为永久缺口，不能更正",
                )
            raise CorrectionError(
                "not_finalized",
                f"序号 {seq} 尚未定稿，只有已定稿字幕允许更正",
            )

        current = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM corrections "
            "WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()["v"]
        if expected_version != current:
            raise CorrectionError(
                "version_conflict",
                f"expected_version={expected_version} 与当前更正版本 {current} 不一致",
            )

        next_version = current + 1
        try:
            row = conn.execute(
                "INSERT INTO corrections (channel, seq, version, text, reason) "
                "VALUES (?, ?, ?, ?, ?) "
                "RETURNING created_at",
                (channel, seq, next_version, text, reason),
            ).fetchone()
        except sqlite3.IntegrityError as exc:  # 极端竞争下的最后防线
            raise CorrectionError(
                "version_conflict",
                f"更正版本 {next_version} 已被并发请求占用",
            ) from exc

        return {
            "channel": channel,
            "seq": seq,
            "correction_version": next_version,
            "text": text,
            "reason": reason,
            "created_at": row["created_at"],
        }


def list_corrections(channel: str, seq: int) -> dict:
    """返回某定稿序号的更正审计记录（按 version 升序）。

    缺口或未定稿序号返回 404——这类序号不存在可审计的更正链。
    """
    with read_tx() as conn:
        finalized = conn.execute(
            "SELECT 1 FROM finalized WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if finalized is None:
            raise CorrectionError(
                "not_finalized",
                f"序号 {seq} 不存在已定稿字幕，没有更正审计记录",
                status_code=404,
            )

        rows = conn.execute(
            "SELECT version, text, reason, created_at FROM corrections "
            "WHERE channel = ? AND seq = ? ORDER BY version",
            (channel, seq),
        ).fetchall()
        return {
            "channel": channel,
            "seq": seq,
            "current_version": rows[-1]["version"] if rows else 0,
            "items": [
                {
                    "channel": channel,
                    "seq": seq,
                    "version": row["version"],
                    "text": row["text"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }


# ---------- 批量更正 ----------


def _batch_request_fingerprint(items: list) -> str:
    """对整批请求按输入顺序做指纹：字段含 seq/expected_version/text/reason。

    text/reason 为经 schema 去除首尾空白后的字符串；JSON 以确定性分隔符
    序列化（ensure_ascii=False、无多余空白），使“完全相同请求”逐字节可比。
    """
    payload = [
        [item["seq"], item["expected_version"], item["text"], item["reason"]]
        for item in items
    ]
    blob = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def submit_correction_batch(channel: str, idempotency_key: str, items: list) -> dict:
    """在单个写事务内原子提交一批（1~100 条）追加式更正。

    先按幂等键查既有批次：完全相同请求（指纹一致）原样返回原批次结果，
    键相同而内容不同抛 idempotency_key_conflict。随后逐项核对——同批
    重复 seq、未定稿序号、缺口序号、expected_version 不匹配均收集为
    冲突项；任一项失败则整批拒绝（不插入任何更正版本），冲突明细按
    输入顺序返回。全部通过才按输入顺序追加各序号的新版本并冻结批次
    记录，返回 batch_id 与逐项结果。
    """
    fingerprint = _batch_request_fingerprint(items)

    with write_tx() as conn:
        existing = conn.execute(
            "SELECT batch_id, created_at, request_fingerprint FROM correction_batches "
            "WHERE channel = ? AND idempotency_key = ?",
            (channel, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise CorrectionError(
                    "idempotency_key_conflict",
                    f"频道 {channel} 的幂等键 {idempotency_key} 已被内容不同的请求占用，"
                    "请更换幂等键后重试",
                )
            return _replay_batch(conn, existing, channel, idempotency_key)

        # 逐项核对：任何一项失败都不写入，冲突按输入顺序收集
        seen: "dict[int, int]" = {}
        conflicts = []
        for index, item in enumerate(items):
            seq = item["seq"]
            if seq in seen:
                conflicts.append(
                    {
                        "index": index,
                        "seq": seq,
                        "error": "duplicate_seq",
                        "message": (
                            f"序号 {seq} 在本批中重复：与第 {seen[seq]} 项"
                            "（下标从 0 起）序号相同"
                        ),
                    }
                )
                continue

            finalized = conn.execute(
                "SELECT 1 FROM finalized WHERE channel = ? AND seq = ?",
                (channel, seq),
            ).fetchone()
            if finalized is None:
                gap = conn.execute(
                    "SELECT 1 FROM gaps WHERE channel = ? AND seq = ?",
                    (channel, seq),
                ).fetchone()
                if gap is not None:
                    err, msg = (
                        "seq_is_gap",
                        f"序号 {seq} 已裁决为永久缺口，不能更正",
                    )
                else:
                    err, msg = (
                        "not_finalized",
                        f"序号 {seq} 尚未定稿，只有已定稿字幕允许更正",
                    )
            else:
                current = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS v FROM corrections "
                    "WHERE channel = ? AND seq = ?",
                    (channel, seq),
                ).fetchone()["v"]
                if item["expected_version"] != current:
                    err, msg = (
                        "version_conflict",
                        f"第 {index} 项 expected_version="
                        f"{item['expected_version']} 与序号 {seq} 的当前更正版本 "
                        f"{current} 不一致",
                    )
                else:
                    err, msg = None, None

            if err is not None:
                conflicts.append(
                    {"index": index, "seq": seq, "error": err, "message": msg}
                )
            else:
                seen[seq] = index

        if conflicts:
            raise CorrectionError(
                "batch_conflict",
                f"{len(conflicts)} 个更正项未通过校验，整批拒绝且无任何写入",
                extra={"conflicts": conflicts},
            )

        # 全部通过：批次行先落库（UNIQUE(channel, idempotency_key) 是并发
        # 重试的最后防线），再按输入顺序逐项追加更正版本并冻结结果
        batch_id = str(uuid.uuid4())
        try:
            conn.execute(
                "INSERT INTO correction_batches "
                "(batch_id, channel, idempotency_key, request_fingerprint) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, channel, idempotency_key, fingerprint),
            )
        except sqlite3.IntegrityError:
            # 极端并发下另一请求已用同键提交：重读既有批次，内容相同则
            # 幂等返回，不同则冲突（本事务随即随异常回滚，零写入）
            existing = conn.execute(
                "SELECT batch_id, created_at, request_fingerprint "
                "FROM correction_batches WHERE channel = ? AND idempotency_key = ?",
                (channel, idempotency_key),
            ).fetchone()
            if existing["request_fingerprint"] == fingerprint:
                return _replay_batch(conn, existing, channel, idempotency_key)
            raise CorrectionError(
                "idempotency_key_conflict",
                f"频道 {channel} 的幂等键 {idempotency_key} 已被内容不同的请求占用，"
                "请更换幂等键后重试",
            )

        results = []
        for index, item in enumerate(items):
            seq = item["seq"]
            next_version = item["expected_version"] + 1
            try:
                row = conn.execute(
                    "INSERT INTO corrections (channel, seq, version, text, reason) "
                    "VALUES (?, ?, ?, ?, ?) RETURNING created_at",
                    (
                        channel,
                        seq,
                        next_version,
                        item["text"],
                        item["reason"],
                    ),
                ).fetchone()
            except sqlite3.IntegrityError as exc:  # 极端竞争下的最后防线
                raise CorrectionError(
                    "version_conflict",
                    f"序号 {seq} 的更正版本 {next_version} 已被并发请求占用，整批拒绝",
                ) from exc
            conn.execute(
                "INSERT INTO correction_batch_items "
                "(batch_id, item_index, seq, expected_version, correction_version, "
                "text, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    index,
                    seq,
                    item["expected_version"],
                    next_version,
                    item["text"],
                    item["reason"],
                    row["created_at"],
                ),
            )
            results.append(
                {
                    "index": index,
                    "seq": seq,
                    "correction_version": next_version,
                    "text": item["text"],
                    "reason": item["reason"],
                    "created_at": row["created_at"],
                }
            )

        created_at = conn.execute(
            "SELECT created_at FROM correction_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()["created_at"]
        return {
            "batch_id": batch_id,
            "channel": channel,
            "idempotency_key": idempotency_key,
            "idempotent": False,
            "created_at": created_at,
            "results": results,
        }


def _replay_batch(conn, batch_row, channel: str, idempotency_key: str) -> dict:
    """按冻结的批次逐项记录构造原样重试响应（只读，不追加任何更正版本）。"""
    rows = conn.execute(
        "SELECT item_index, seq, correction_version, text, reason, created_at "
        "FROM correction_batch_items WHERE batch_id = ? ORDER BY item_index",
        (batch_row["batch_id"],),
    ).fetchall()
    return {
        "batch_id": batch_row["batch_id"],
        "channel": channel,
        "idempotency_key": idempotency_key,
        "idempotent": True,
        "created_at": batch_row["created_at"],
        "results": [
            {
                "index": row["item_index"],
                "seq": row["seq"],
                "correction_version": row["correction_version"],
                "text": row["text"],
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    }


# ---------- 发布快照 ----------


def create_snapshot(channel: str, idempotency_key: str) -> dict:
    """为频道创建发布快照：单事务锁定当前 watermark 并冻结 1..watermark。

    同一 (channel, idempotency_key) 重试不重新冻结，返回既有快照
    （idempotent=true）。不存在的频道生成 watermark=0 的空快照，且
    不会在 channels 表中创建频道行。
    """
    with write_tx() as conn:
        existing = conn.execute(
            "SELECT snapshot_id, watermark, created_at FROM snapshots "
            "WHERE channel = ? AND idempotency_key = ?",
            (channel, idempotency_key),
        ).fetchone()
        if existing is not None:
            return _snapshot_meta(
                existing, channel, idempotency_key, idempotent=True
            )

        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0

        snapshot_id = str(uuid.uuid4())
        try:
            conn.execute(
                "INSERT INTO snapshots "
                "(snapshot_id, channel, watermark, idempotency_key) "
                "VALUES (?, ?, ?, ?)",
                (snapshot_id, channel, watermark, idempotency_key),
            )
        except sqlite3.IntegrityError:
            # 极端并发下同一幂等键竞争：唯一约束是最后防线，转幂等返回
            existing = conn.execute(
                "SELECT snapshot_id, watermark, created_at FROM snapshots "
                "WHERE channel = ? AND idempotency_key = ?",
                (channel, idempotency_key),
            ).fetchone()
            return _snapshot_meta(
                existing, channel, idempotency_key, idempotent=True
            )

        if watermark > 0:
            # 字幕行：同时固化原始文本与创建时刻的生效文本（最新更正）
            conn.execute(
                """
                INSERT INTO snapshot_items (
                    snapshot_id, seq, kind, revision, original_text,
                    effective_text, correction_version, start_seq, end_seq
                )
                SELECT ?, f.seq, 'subtitle', f.revision, f.text,
                       COALESCE(c.text, f.text), COALESCE(c.version, 0),
                       NULL, NULL
                FROM finalized f
                LEFT JOIN corrections c
                       ON c.channel = f.channel AND c.seq = f.seq
                      AND c.version = (
                          SELECT MAX(c2.version) FROM corrections c2
                          WHERE c2.channel = f.channel AND c2.seq = f.seq
                      )
                WHERE f.channel = ? AND f.seq BETWEEN 1 AND ?
                """,
                (snapshot_id, channel, watermark),
            )
            # 缺口行：固化所属裁决区间端点
            conn.execute(
                """
                INSERT INTO snapshot_items (
                    snapshot_id, seq, kind, revision, original_text,
                    effective_text, correction_version, start_seq, end_seq
                )
                SELECT ?, seq, 'gap', NULL, NULL, NULL, NULL, start_seq, end_seq
                FROM gaps
                WHERE channel = ? AND seq BETWEEN 1 AND ?
                """,
                (snapshot_id, channel, watermark),
            )
            # 各条更正版本完整冻结（按版本升序，供快照内审计）
            conn.execute(
                """
                INSERT INTO snapshot_corrections
                    (snapshot_id, seq, version, text, reason, created_at)
                SELECT ?, seq, version, text, reason, created_at
                FROM corrections
                WHERE channel = ? AND seq BETWEEN 1 AND ?
                """,
                (snapshot_id, channel, watermark),
            )

        created_at = conn.execute(
            "SELECT created_at FROM snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()["created_at"]
        return {
            "snapshot_id": snapshot_id,
            "channel": channel,
            "watermark": watermark,
            "idempotency_key": idempotency_key,
            "idempotent": False,
            "created_at": created_at,
        }


def _snapshot_meta(row, channel: str, idempotency_key: str, idempotent: bool) -> dict:
    """从 snapshots 行构造创建/重试响应。"""
    return {
        "snapshot_id": row["snapshot_id"],
        "channel": channel,
        "watermark": row["watermark"],
        "idempotency_key": idempotency_key,
        "idempotent": idempotent,
        "created_at": row["created_at"],
    }


def _get_snapshot_meta(conn, snapshot_id: str):
    meta = conn.execute(
        "SELECT channel, watermark FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if meta is None:
        raise SnapshotError(
            "snapshot_not_found",
            f"快照 {snapshot_id} 不存在",
            status_code=404,
        )
    return meta


def get_snapshot(
    snapshot_id: str, after: int = 0, limit: int = 1000, view: str = "original"
) -> dict:
    """按 snapshot_id 分页拉取快照条目（after/limit 按序号）。

    返回的是快照时刻冻结的内容：快照创建后新定稿的字幕、新追加的更正
    或新裁决的缺口都不会影响任何既有快照。
    """
    with read_tx() as conn:
        meta = _get_snapshot_meta(conn, snapshot_id)
        channel, watermark = meta["channel"], meta["watermark"]

        if view == "original":
            rows = conn.execute(
                """
                SELECT seq, kind, revision, original_text, start_seq, end_seq
                FROM snapshot_items
                WHERE snapshot_id = ? AND seq > ?
                ORDER BY seq LIMIT ?
                """,
                (snapshot_id, after, limit),
            ).fetchall()
            return {
                "snapshot_id": snapshot_id,
                "channel": channel,
                "watermark": watermark,
                "view": "original",
                "items": [_snapshot_item_dict(row, effective=False) for row in rows],
            }

        rows = conn.execute(
            """
            SELECT seq, kind, revision, original_text, effective_text,
                   correction_version, start_seq, end_seq
            FROM snapshot_items
            WHERE snapshot_id = ? AND seq > ?
            ORDER BY seq LIMIT ?
            """,
            (snapshot_id, after, limit),
        ).fetchall()
        items = []
        page_max_version = 0
        for row in rows:
            item = _snapshot_item_dict(row, effective=True)
            if item["correction_version"] is not None:
                page_max_version = max(
                    page_max_version, item["correction_version"]
                )
            items.append(item)
        return {
            "snapshot_id": snapshot_id,
            "channel": channel,
            "watermark": watermark,
            "view": "effective",
            "correction_version": page_max_version,
            "items": items,
        }


def _snapshot_item_dict(row, effective: bool) -> dict:
    """将快照条目行转为响应 dict；effective=True 时返回冻结的生效文本。"""
    if row["kind"] == "gap":
        return {
            "kind": "gap",
            "seq": row["seq"],
            "revision": None,
            "text": None,
            "start_seq": row["start_seq"],
            "end_seq": row["end_seq"],
            **(
                {"correction_version": None}
                if effective
                else {}
            ),
        }
    item = {
        "kind": "subtitle",
        "seq": row["seq"],
        "revision": row["revision"],
        "text": row["effective_text"] if effective else row["original_text"],
        "start_seq": None,
        "end_seq": None,
    }
    if effective:
        item["correction_version"] = row["correction_version"]
    return item


def list_snapshot_corrections(snapshot_id: str, seq: int) -> dict:
    """返回快照内某字幕序号冻结的更正历史（按 version 升序）。

    快照不存在 -> 404 snapshot_not_found；序号超出快照锁定水位线 ->
    404 seq_out_of_snapshot；序号在快照中是缺口 -> 409 seq_is_gap。
    """
    with read_tx() as conn:
        meta = _get_snapshot_meta(conn, snapshot_id)
        channel, watermark = meta["channel"], meta["watermark"]

        if seq < 1 or seq > watermark:
            raise SnapshotError(
                "seq_out_of_snapshot",
                f"序号 {seq} 超出快照 {snapshot_id} 的水位线 {watermark}",
                status_code=404,
            )

        item = conn.execute(
            "SELECT kind FROM snapshot_items WHERE snapshot_id = ? AND seq = ?",
            (snapshot_id, seq),
        ).fetchone()
        if item["kind"] == "gap":
            raise SnapshotError(
                "seq_is_gap",
                f"序号 {seq} 在快照 {snapshot_id} 中是永久缺口，没有更正历史",
                status_code=409,
            )

        rows = conn.execute(
            "SELECT version, text, reason, created_at FROM snapshot_corrections "
            "WHERE snapshot_id = ? AND seq = ? ORDER BY version",
            (snapshot_id, seq),
        ).fetchall()
        return {
            "snapshot_id": snapshot_id,
            "channel": channel,
            "seq": seq,
            "current_version": rows[-1]["version"] if rows else 0,
            "items": [
                {
                    "seq": seq,
                    "version": row["version"],
                    "text": row["text"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }


def diff_snapshots(
    snapshot_id: str, other_snapshot_id: str, after: int = 0, limit: int = 1000
) -> dict:
    """比较同一频道两个快照的冻结生效内容，按 seq 合并返回差异。

    路径快照为基准（base），other_snapshot_id 为对方（other）：
    - added：seq 只存在于对方快照（基准冻结水位线之外的新内容）；
    - removed：seq 只存在于基准快照（对方是更早快照时才会出现）；
    - changed：两侧都有但冻结内容不同——字幕以生效文本（effective_text）
      与更正版本（correction_version）判定，缺口以裁决区间端点
      (start_seq, end_seq) 判定，kind 不同同样视为变更。
    两侧完全一致的序号不返回。after/limit 只对差异条目计数，next_after
    为本页最后一条差异的 seq（无差异时为 None），供下一页续拉。
    """
    with read_tx() as conn:
        base = _get_snapshot_meta(conn, snapshot_id)
        other = conn.execute(
            "SELECT channel, watermark FROM snapshots WHERE snapshot_id = ?",
            (other_snapshot_id,),
        ).fetchone()
        if other is None:
            raise SnapshotError(
                "snapshot_not_found",
                f"快照 {other_snapshot_id} 不存在",
                status_code=404,
            )
        if other["channel"] != base["channel"]:
            raise SnapshotError(
                "snapshot_channel_mismatch",
                f"快照 {snapshot_id}（频道 {base['channel']}）与快照 "
                f"{other_snapshot_id}（频道 {other['channel']}）不属于同一频道，"
                "不能比较",
                status_code=409,
            )

        # FULL OUTER JOIN 由两段 LEFT JOIN 的 UNION ALL 模拟：第一段覆盖
        # “基准有 / 双方都有（且有差异）”，第二段补齐“仅对方有”。
        # 差异过滤与 LIMIT 都落在合并后的 seq 序列上，因此未变化条目
        # 不占 limit，再稀疏的差异也会被继续扫描出来。
        # IS NOT 是 SQLite 的 NULL 安全不等号（NULL IS NOT NULL -> 0）。
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT b.seq                  AS seq,
                       b.kind                 AS base_kind,
                       b.revision             AS base_revision,
                       b.effective_text       AS base_effective_text,
                       b.correction_version   AS base_correction_version,
                       b.start_seq            AS base_start_seq,
                       b.end_seq              AS base_end_seq,
                       o.kind                 AS other_kind,
                       o.revision             AS other_revision,
                       o.effective_text       AS other_effective_text,
                       o.correction_version   AS other_correction_version,
                       o.start_seq            AS other_start_seq,
                       o.end_seq              AS other_end_seq
                FROM snapshot_items b
                LEFT JOIN snapshot_items o
                       ON o.snapshot_id = ? AND o.seq = b.seq
                WHERE b.snapshot_id = ? AND b.seq > ?
                  AND (o.seq IS NULL
                       OR b.kind               IS NOT o.kind
                       OR b.effective_text     IS NOT o.effective_text
                       OR b.correction_version IS NOT o.correction_version
                       OR b.start_seq          IS NOT o.start_seq
                       OR b.end_seq            IS NOT o.end_seq)
                UNION ALL
                SELECT o.seq                  AS seq,
                       NULL, NULL, NULL, NULL, NULL, NULL,
                       o.kind                 AS other_kind,
                       o.revision             AS other_revision,
                       o.effective_text       AS other_effective_text,
                       o.correction_version   AS other_correction_version,
                       o.start_seq            AS other_start_seq,
                       o.end_seq              AS other_end_seq
                FROM snapshot_items o
                LEFT JOIN snapshot_items b
                       ON b.snapshot_id = ? AND b.seq = o.seq
                WHERE o.snapshot_id = ? AND o.seq > ?
                  AND b.seq IS NULL
            )
            ORDER BY seq
            LIMIT ?
            """,
            (
                other_snapshot_id, snapshot_id, after,
                snapshot_id, other_snapshot_id, after,
                limit,
            ),
        ).fetchall()

        items = []
        for row in rows:
            base_item = _diff_side_item(row, "base")
            other_item = _diff_side_item(row, "other")
            if base_item is None:
                change = "added"
            elif other_item is None:
                change = "removed"
            else:
                change = "changed"
            items.append(
                {
                    "change": change,
                    "seq": row["seq"],
                    "base": base_item,
                    "other": other_item,
                }
            )

        return {
            "snapshot_id": snapshot_id,
            "other_snapshot_id": other_snapshot_id,
            "channel": base["channel"],
            "watermark": base["watermark"],
            "other_watermark": other["watermark"],
            "items": items,
            "next_after": items[-1]["seq"] if items else None,
        }


def _diff_side_item(row, side: str):
    """从差异查询的 base_* / other_* 列构造一侧的冻结生效条目；该侧无此 seq 时为 None。"""
    kind = row[f"{side}_kind"]
    if kind is None:
        return None
    seq = row["seq"]
    if kind == "gap":
        return {
            "kind": "gap",
            "seq": seq,
            "revision": None,
            "text": None,
            "start_seq": row[f"{side}_start_seq"],
            "end_seq": row[f"{side}_end_seq"],
            "correction_version": None,
        }
    return {
        "kind": "subtitle",
        "seq": seq,
        "revision": row[f"{side}_revision"],
        "text": row[f"{side}_effective_text"],
        "start_seq": None,
        "end_seq": None,
        "correction_version": row[f"{side}_correction_version"],
    }


# ---------- 快照敏感词遮盖 ----------


def redact_snapshot(
    snapshot_id: str, words: list, after: int = 0, limit: int = 100
) -> dict:
    """对快照冻结的生效文本做敏感词遮盖，按 after/limit 分页返回（只读）。

    遮盖在整个快照范围内一次算好再分页：1..watermark 的相邻字幕（中间
    没有缺口）按序号无分隔拼接，缺口阻断拼接；在去重后的词表上逐码点、
    区分大小写地枚举全部重叠命中，覆盖区间取并集后每个被覆盖码点替换
    为一个 "*"（长度不变），再按各字幕原码点长度拆回原序号。快照表与
    定稿 / 更正原表均不被修改。
    """
    # 防御性去重（HTTP 层 schema 已去重并校验 1~20 项、每项 1~128 码点）
    words = list(dict.fromkeys(words))
    with read_tx() as conn:
        meta = _get_snapshot_meta(conn, snapshot_id)
        channel, watermark = meta["channel"], meta["watermark"]
        rows = conn.execute(
            """
            SELECT seq, kind, revision, effective_text, correction_version,
                   start_seq, end_seq
            FROM snapshot_items
            WHERE snapshot_id = ?
            ORDER BY seq
            """,
            (snapshot_id,),
        ).fetchall()

    masked = _redact_texts(rows, words)

    items = []
    has_more = False
    for row in rows:
        if row["seq"] <= after:
            continue
        if len(items) >= limit:
            has_more = True
            break
        if row["kind"] == "gap":
            items.append(
                {
                    "kind": "gap",
                    "seq": row["seq"],
                    "revision": None,
                    "text": None,
                    "start_seq": row["start_seq"],
                    "end_seq": row["end_seq"],
                    "correction_version": None,
                }
            )
        else:
            items.append(
                {
                    "kind": "subtitle",
                    "seq": row["seq"],
                    "revision": row["revision"],
                    "text": masked[row["seq"]],
                    "start_seq": None,
                    "end_seq": None,
                    "correction_version": row["correction_version"],
                }
            )

    return {
        "snapshot_id": snapshot_id,
        "channel": channel,
        "watermark": watermark,
        "words": words,
        "items": items,
        "next_after": items[-1]["seq"] if (items and has_more) else None,
    }


def _redact_texts(rows: list, words: list) -> dict:
    """按序号拼接相邻字幕并计算遮盖，返回 {seq: 遮盖后文本}。

    缺口行截断当前拼接段，敏感词不会跨缺口命中。遮盖后文本与原文码点
    数相同（每个被覆盖码点恰好换成一个 "*"），按原长度拆回各序号。
    """
    masked = {}
    segment_seqs = []
    segment_texts = []

    def flush():
        if not segment_seqs:
            return
        joined = _mask_text("".join(segment_texts), words)
        pos = 0
        for seq, text in zip(segment_seqs, segment_texts):
            masked[seq] = joined[pos:pos + len(text)]
            pos += len(text)
        segment_seqs.clear()
        segment_texts.clear()

    for row in rows:
        if row["kind"] == "gap":
            flush()
            continue
        segment_seqs.append(row["seq"])
        segment_texts.append(row["effective_text"])
    flush()
    return masked


def _mask_text(text: str, words: list) -> str:
    """把 text 中被任一敏感词覆盖的码点替换为 "*"（重叠命中取并集）。"""
    if not text or not words:
        return text
    covered = [False] * len(text)
    for word in words:
        start = 0
        while True:
            index = text.find(word, start)
            if index < 0:
                break
            for i in range(index, index + len(word)):
                covered[i] = True
            start = index + 1  # 每次只前进一步，重叠命中也被枚举
    return "".join("*" if covered[i] else ch for i, ch in enumerate(text))


# ---------- 剪辑字幕导出 ----------


def export_snapshot_edit(snapshot_id: str, timings: list, clips: list) -> dict:
    """按剪辑时间轴导出快照字幕 cue（纯只读，不修改快照）。

    timings 须恰好覆盖快照内全部字幕 seq（无缺口、无重复、无多余），
    否则 422 timings_mismatch；clips 按输入顺序首尾拼接为成片时间轴，
    每条字幕与每个 clip 取左闭右开交集，非空才生成 cue 并平移到成片
    累计时间——一条字幕横跨多个 clip 时按交集拆分，重复 clip 在不同
    偏移处保留副本，时间重叠的字幕各自生成 cue 不合并。cue 按
    (start_ms, clip_index, seq) 升序返回。
    """
    with read_tx() as conn:
        meta = _get_snapshot_meta(conn, snapshot_id)
        rows = conn.execute(
            "SELECT seq, effective_text FROM snapshot_items "
            "WHERE snapshot_id = ? AND kind = 'subtitle' ORDER BY seq",
            (snapshot_id,),
        ).fetchall()
    texts = {row["seq"]: row["effective_text"] for row in rows}

    # timings 须恰好覆盖全部字幕 seq：无重复、无缺口、无多余
    timing_by_seq = {}
    for timing in timings:
        seq = timing["seq"]
        if seq in timing_by_seq:
            raise SnapshotError(
                "timings_mismatch",
                f"timings 中序号 {seq} 重复：每个字幕 seq 须恰好出现一次",
                status_code=422,
            )
        timing_by_seq[seq] = timing
    missing = sorted(set(texts) - set(timing_by_seq))
    extra = sorted(set(timing_by_seq) - set(texts))
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"缺少字幕序号 {missing}")
        if extra:
            parts.append(f"包含快照中不存在的字幕序号 {extra}")
        raise SnapshotError(
            "timings_mismatch",
            "timings 须恰好覆盖快照全部字幕 seq（无缺口、无多余）："
            + "；".join(parts),
            status_code=422,
        )

    cues = []
    offset = 0  # 当前 clip 在成片时间轴上的起点（此前所有 clip 时长之和）
    for clip_index, clip in enumerate(clips):
        clip_start, clip_end = clip["start_ms"], clip["end_ms"]
        for timing in timings:
            start = max(timing["start_ms"], clip_start)
            end = min(timing["end_ms"], clip_end)
            if start < end:  # 左闭右开交集非空才生成 cue
                cues.append(
                    {
                        "seq": timing["seq"],
                        "clip_index": clip_index,
                        "text": texts[timing["seq"]],
                        "start_ms": start - clip_start + offset,
                        "end_ms": end - clip_start + offset,
                    }
                )
        offset += clip_end - clip_start

    cues.sort(key=lambda cue: (cue["start_ms"], cue["clip_index"], cue["seq"]))
    return {
        "snapshot_id": snapshot_id,
        "channel": meta["channel"],
        "format": "json",
        "duration_ms": offset,
        "cues": cues,
    }


def render_vtt(cues: list) -> str:
    """把排序后的 cue 列表渲染为合法 WebVTT 文档（正文转义 & < >）。"""
    lines = ["WEBVTT", ""]
    for number, cue in enumerate(cues, 1):
        lines.append(str(number))
        lines.append(
            f"{_vtt_timestamp(cue['start_ms'])} --> {_vtt_timestamp(cue['end_ms'])}"
        )
        lines.append(_escape_vtt_text(cue["text"]))
        lines.append("")
    return "\n".join(lines) + "\n"


def _vtt_timestamp(ms: int) -> str:
    """毫秒转 WebVTT 时间戳 HH:MM:SS.mmm。"""
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _escape_vtt_text(text: str) -> str:
    """WebVTT 正文转义：& < > 替换为实体，避免被解析为标记或时间戳箭头。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- 快照字幕引用迁移 ----------


def migrate_snapshot_quote(
    source_snapshot_id: str,
    target_snapshot_id: str,
    start_seq: int,
    start_offset: int,
    end_seq: int,
    end_offset: int,
) -> dict:
    """把源快照中的一段非空引用迁移定位到目标快照（纯只读，不修改任何状态）。

    引用区间按 [start_seq, end_seq]（含两端）+ 起止字幕内码点偏移
    （左闭右开）给定，按源快照冻结的生效文本提取：引用可跨越相邻
    （中间无缺口）字幕，但不得越过缺口；序号 / 偏移无效或引用为空均
    以 422 invalid_quote_range 拒绝。随后在同频道目标快照的连续字幕
    文本中逐码点查找完整引用（相邻字幕无分隔拼接、缺口阻断）：恰好
    一次返回 found 及对应起止序号 / 偏移；零次返回 missing；多次返回
    ambiguous（绝不任选一个位置）。
    """
    with read_tx() as conn:
        source = _get_snapshot_meta(conn, source_snapshot_id)
        target = conn.execute(
            "SELECT channel, watermark FROM snapshots WHERE snapshot_id = ?",
            (target_snapshot_id,),
        ).fetchone()
        if target is None:
            raise SnapshotError(
                "snapshot_not_found",
                f"快照 {target_snapshot_id} 不存在",
                status_code=404,
            )
        if target["channel"] != source["channel"]:
            raise SnapshotError(
                "snapshot_channel_mismatch",
                f"源快照 {source_snapshot_id}（频道 {source['channel']}）与目标快照 "
                f"{target_snapshot_id}（频道 {target['channel']}）不属于同一频道，"
                "不能迁移引用",
                status_code=409,
            )

        rows = conn.execute(
            """
            SELECT snapshot_id AS sid, seq, kind, effective_text
            FROM snapshot_items
            WHERE snapshot_id IN (?, ?)
            ORDER BY snapshot_id, seq
            """,
            (source_snapshot_id, target_snapshot_id),
        ).fetchall()
    source_by_seq = {
        row["seq"]: row for row in rows if row["sid"] == source_snapshot_id
    }
    target_rows = [row for row in rows if row["sid"] == target_snapshot_id]

    quote = _extract_source_quote(
        source_by_seq, source["watermark"],
        start_seq, start_offset, end_seq, end_offset,
    )

    matches = _find_quote_matches(target_rows, quote)
    result = {
        "source_snapshot_id": source_snapshot_id,
        "target_snapshot_id": target_snapshot_id,
        "channel": source["channel"],
        "status": None,
        "source": {
            "start_seq": start_seq,
            "start_offset": start_offset,
            "end_seq": end_seq,
            "end_offset": end_offset,
        },
        "quote": quote,
        "match": None,
    }
    if len(matches) == 1:
        result["status"] = "found"
        result["match"] = matches[0]
    elif not matches:
        result["status"] = "missing"
    else:
        result["status"] = "ambiguous"
    return result


def _invalid_quote_range(message: str) -> SnapshotError:
    """构造引用区间非法的 422 业务错误。"""
    return SnapshotError("invalid_quote_range", message, status_code=422)


def _extract_source_quote(
    source_by_seq: dict,
    source_watermark: int,
    start_seq: int,
    start_offset: int,
    end_seq: int,
    end_offset: int,
) -> str:
    """按序号区间与码点偏移（左闭右开）从源快照冻结生效文本提取引用。

    区间 [start_seq, end_seq] 内每个序号都必须是源快照中的字幕行
    （超界 / 缺口 / 空洞均拒绝）；start_offset 落在 start_seq 文本内、
    end_offset 落在 end_seq 文本内（均可等于文本长度）；按首条取后缀、
    末条取前缀、中间整条拼接后，引用不得为空。
    """
    if not (1 <= start_seq <= source_watermark) or not (
        1 <= end_seq <= source_watermark
    ):
        raise _invalid_quote_range(
            f"引用序号超出源快照水位线 {source_watermark}："
            f"{start_seq}..{end_seq}"
        )

    texts = []
    for seq in range(start_seq, end_seq + 1):
        row = source_by_seq.get(seq)
        if row is None or row["kind"] != "subtitle":
            raise _invalid_quote_range(
                f"序号 {seq} 在源快照中不是字幕（缺口或缺失），"
                "引用不得越过缺口"
            )
        texts.append(row["effective_text"])

    if not 0 <= start_offset <= len(texts[0]):
        raise _invalid_quote_range(
            f"起始偏移 {start_offset} 超出序号 {start_seq} 文本长度 "
            f"{len(texts[0])}（左闭右开，允许等于文本长度）"
        )
    if not 0 <= end_offset <= len(texts[-1]):
        raise _invalid_quote_range(
            f"终点偏移 {end_offset} 超出序号 {end_seq} 文本长度 "
            f"{len(texts[-1])}（左闭右开，允许等于文本长度）"
        )

    if start_seq == end_seq:
        if start_offset >= end_offset:
            raise _invalid_quote_range(
                "同一字幕内 start_offset 必须小于 end_offset（左闭右开，"
                "引用不得为空）"
            )
        return texts[0][start_offset:end_offset]

    # 跨字幕：首条截取起点之后、末条截取终点之前，中间整条（含可能为空
    # 的字幕，空字幕仍属于同一连续段）保留；最终拼接为空（如起止偏移都
    # 为 0，或恰好在字幕边界取了零宽区间）一律拒绝。
    pieces = [texts[0][start_offset:]]
    pieces.extend(texts[1:-1])
    pieces.append(texts[-1][:end_offset])
    quote = "".join(pieces)
    if not quote:
        raise _invalid_quote_range(
            "按给定序号区间与码点偏移提取出的引用为空，引用不得为空"
        )
    return quote


def _find_quote_matches(target_rows: list, quote: str) -> list:
    """在目标快照的连续字幕拼接文本中枚举引用的全部出现（最多保留 2 个）。

    相邻字幕（中间没有缺口）按序号无分隔拼接为一段，缺口截断分段；
    匹配逐码点、区分大小写，每次命中后只前进一位（重叠出现也枚举）。
    返回至多 2 个命中的 {start_seq, start_offset, end_seq, end_offset}，
    调用方据此区分 found（1）/ missing（0）/ ambiguous（>1）。
    """
    matches = []
    segment = []  # [(seq, text), ...]，当前无缺口连续字幕段
    for row in target_rows:
        if row["kind"] == "gap":
            matches.extend(_matches_in_segment(segment, quote, 2 - len(matches)))
            segment = []
            if len(matches) >= 2:
                return matches
            continue
        segment.append((row["seq"], row["effective_text"]))
    matches.extend(_matches_in_segment(segment, quote, 2 - len(matches)))
    return matches[:2]


def _matches_in_segment(segment: list, quote: str, limit: int) -> list:
    """在一条连续字幕段中枚举至多 limit 个引用命中并映射回序号 / 偏移。"""
    if not segment or limit <= 0:
        return []
    seqs = [seq for seq, _ in segment]
    texts = [text for _, text in segment]
    joined = "".join(texts)
    boundaries = []
    pos = 0
    for text in texts:
        boundaries.append(pos)
        pos += len(text)

    def locate_start(index: int) -> "tuple[int, int]":
        """拼接文本码点下标 -> 起点（所属序号, 序号内码点偏移）。

        下标落在某字幕起点上时优先归属该字幕（空字幕重复边界则归属之后
        第一条非空字幕）——跨字幕边界的命中，其起点确定映射到引用第一
        个码点所属的字幕。
        """
        for j in range(len(seqs) - 1, -1, -1):
            if boundaries[j] <= index:
                return seqs[j], index - boundaries[j]
        return seqs[0], 0

    def locate_end(index: int) -> "tuple[int, int]":
        """拼接文本码点下标（不含）-> 终点（所属序号, 序号内码点偏移）。

        终点归属于引用最后一个码点（index-1）所在的字幕：取最后一个
        起点严格小于 index 的字幕，因此恰好落在字幕边界上的终点映射为
        前一条字幕的文本长度，而不是后一条字幕的偏移 0；前一条为空时
        继续向前归属（空字幕不占终点）。
        """
        for j in range(len(seqs) - 1, -1, -1):
            if boundaries[j] < index:
                return seqs[j], index - boundaries[j]
        return seqs[0], 0

    found = []
    search_from = 0
    while len(found) < limit:
        index = joined.find(quote, search_from)
        if index < 0:
            break
        start_seq_, start_off = locate_start(index)
        end_seq_, end_off = locate_end(index + len(quote))
        found.append(
            {
                "start_seq": start_seq_,
                "start_offset": start_off,
                "end_seq": end_seq_,
                "end_offset": end_off,
            }
        )
        search_from = index + 1  # 每次只前进一步，重叠出现也被枚举
    return found


# ---------- 稿件校稿对齐 ----------


def align_script(snapshot_id: str, lines: list) -> dict:
    """把稿件行与快照冻结字幕做全局最小代价顺序对齐（纯只读，不修改状态）。

    lines 为 [{"text": ..., "max_subtitles": ...}, ...]（HTTP 层已校验
    1~100 条、文本非空、max_subtitles∈1..3）。只读取快照中 kind=subtitle
    的冻结生效文本，按序号升序枚举；每行可匹配同一连续段内（中间无缺口）
    的 1..max_subtitles 条连续字幕，字幕块无分隔拼接；稿件行 / 字幕也可
    跳过，跳过代价为各自文本码点数。全局最优的并列裁决依次为：跳过项数
    更少、合并字幕数（Σ(k-1)）更少、匹配序号字典序更小。
    """
    with read_tx() as conn:
        meta = _get_snapshot_meta(conn, snapshot_id)
        channel, watermark = meta["channel"], meta["watermark"]
        rows = conn.execute(
            "SELECT seq, effective_text FROM snapshot_items "
            "WHERE snapshot_id = ? AND kind = 'subtitle' ORDER BY seq",
            (snapshot_id,),
        ).fetchall()

    sub_seqs = [row["seq"] for row in rows]
    sub_texts = [row["effective_text"] for row in rows]
    line_texts = [line["text"] for line in lines]
    line_max_k = [line["max_subtitles"] for line in lines]

    total_cost, _, backtrack = _script_alignment_dp(
        line_texts, line_max_k, sub_seqs, sub_texts
    )

    # 回溯重建完整对齐：path 为按顺序的操作序列
    #   ("match", line_index, start_index, end_index_inclusive, distance)
    #   ("skip_line", line_index)
    #   ("skip_sub", subtitle_index)
    # DP 内的匹配序列 Trie 节点只用于构造期字典序裁决；明细与未匹配
    # 列表统一从回溯路径派生，保证单一事实来源。
    path = []
    i, j = len(lines), len(sub_seqs)
    while i > 0 or j > 0:
        code = backtrack[i][j]
        if code >= _BT_MATCH_BASE:
            k = code - _BT_MATCH_BASE + 1
            i -= 1
            j -= k
            text = "".join(sub_texts[j:j + k])
            path.append(("match", i, j, j + k - 1,
                         _codepoint_edit_distance(line_texts[i], text)))
        elif code == _BT_SKIP_LINE:
            i -= 1
            path.append(("skip_line", i))
        else:  # _BT_SKIP_SUB
            j -= 1
            path.append(("skip_sub", j))
    path.reverse()

    alignments = [
        {
            "index": idx,
            "matched": False,
            "start_seq": None,
            "end_seq": None,
            "subtitles": None,
            "distance": None,
        }
        for idx in range(len(lines))
    ]
    unmatched_lines = []
    unmatched_seqs = []
    for op in path:
        if op[0] == "match":
            _, li, sj, ej, dist = op
            alignments[li] = {
                "index": li,
                "matched": True,
                "start_seq": sub_seqs[sj],
                "end_seq": sub_seqs[ej],
                "subtitles": ej - sj + 1,
                "distance": dist,
            }
        elif op[0] == "skip_line":
            unmatched_lines.append(op[1])
        else:
            unmatched_seqs.append(sub_seqs[op[1]])

    return {
        "snapshot_id": snapshot_id,
        "channel": channel,
        "watermark": watermark,
        "total_cost": total_cost,
        "alignments": alignments,
        "unmatched_script_lines": unmatched_lines,
        "unmatched_subtitle_seqs": unmatched_seqs,
    }


# 回溯码：0=跳过字幕，1=跳过稿件行，>=2 表示匹配 k=码值-_BT_MATCH_BASE+1 条
_BT_SKIP_SUB = 0
_BT_SKIP_LINE = 1
_BT_MATCH_BASE = 2


class _MatchSeqNode:
    """匹配块序号对序列的持久化 Trie 节点（字典序并列裁决用）。

    根节点 pair 为 None；每个匹配操作经 parent.children 取或建
    (parent, (start_seq, end_seq)) 子节点。节点不可变，同一父节点的
    相同子标签复用既有节点；子节点缓存挂在节点上，随行滚动自然回收。
    """

    __slots__ = ("parent", "pair", "depth", "children")

    def __init__(self, parent: "_MatchSeqNode | None", pair):
        self.parent = parent
        self.pair = pair
        self.depth = 0 if parent is None else parent.depth + 1
        self.children = None  # 惰性创建：{pair: node}

    def child(self, pair):
        """取标签为 pair 的子节点，不存在则创建（同标签幂等复用）。"""
        table = self.children
        if table is None:
            node = _MatchSeqNode(self, pair)
            self.children = {pair: node}
            return node
        node = table.get(pair)
        if node is None:
            node = _MatchSeqNode(self, pair)
            table[pair] = node
        return node


def _script_alignment_dp(line_texts, line_max_k, sub_seqs, sub_texts):
    """稿件行 × 字幕顺序对齐的二维动态规划。

    状态 (i, j)：前 i 条稿件行与字幕有序列表前 j 个位置（按字幕下标，
    非快照序号）对齐完成。入边三种：
      - 跳过字幕 j-1：代价 len(sub_texts[j-1])；
      - 跳过稿件行 i-1：代价 len(line_texts[i-1])；
      - 行 i-1 匹配字幕块 [j-k, j)：k∈1..max，块内序号连续（中间无
        缺口 / 空洞），代价为行文本与块拼接文本的码点编辑距离。
    单元按 (总代价, 跳过项数, 合并字幕数, 匹配序号字典序) 四元组取小。
    返回 (终态总代价, 终态匹配序列 Trie 节点, 回溯码二维表)。
    """
    n, m = len(line_texts), len(sub_seqs)
    sub_lens = [len(t) for t in sub_texts]
    line_lens = [len(t) for t in line_texts]

    # 预计算各起点最多 max(line_max_k) 条连续字幕的拼接文本；
    # 序号不连续（中间有缺口）的块不存在，且更长的块必然同跨该缺口。
    max_k_global = max(line_max_k)
    joins = {}  # (start_index, k) -> 拼接文本，仅覆盖序号连续的块
    for s in range(m):
        buf = ""
        for k in range(1, min(max_k_global, m - s) + 1):
            if k > 1 and sub_seqs[s + k - 1] != sub_seqs[s + k - 2] + 1:
                break
            buf += sub_texts[s + k - 1]
            joins[(s, k)] = buf

    root = _MatchSeqNode(None, None)

    # 单元四元组：(cost, skips, joins_count, node)；滚动两行 prev / cur
    prev = [None] * (m + 1)
    prev[0] = (0, 0, 0, root)
    acc = 0
    for j in range(1, m + 1):  # i = 0：只能逐条跳过字幕
        acc += sub_lens[j - 1]
        prev[j] = (acc, j, 0, root)

    backtrack = [[_BT_SKIP_LINE] * (m + 1) for _ in range(n + 1)]
    backtrack[0][0] = _BT_SKIP_SUB  # (0,0) 不会被回溯，占位即可
    for j in range(1, m + 1):
        backtrack[0][j] = _BT_SKIP_SUB

    for i in range(1, n + 1):
        li_text = line_texts[i - 1]
        li_len = line_lens[i - 1]

        # LCP 缓存按行重置：缓存的是“本行深度”节点对的最长公共前缀，
        # 下一行节点深度变化后旧缓存不再适用。Trie 子节点表挂在节点上
        # 全局复用（不同行相同路径前缀共享同一节点）。
        lcp_cache = {}  # (id(a), id(b)) -> lcp_depth

        def lex_cmp(a, b):
            """匹配序列字典序：-1 a 小 / 0 相等 / 1 b 小（仅比较等长序列）。"""
            if a is b:
                return 0
            key = (id(a), id(b))
            depth = lcp_cache.get(key)
            if depth is None:
                depth = 0
                ta, tb = a, b
                while ta.pair is not None and ta.pair == tb.pair:
                    depth += 1
                    ta, tb = ta.parent, tb.parent
                # 沿途祖先对的 LCP 恰递减 1，一并记录到分叉点为止
                step = 0
                da, db_node = a, b
                while step < depth:
                    lcp_cache[(id(da), id(db_node))] = depth - step
                    da, db_node = da.parent, db_node.parent
                    step += 1
            if depth == a.depth:
                return 0  # 完全相同
            # 上溯 depth 层后到达第一个不同标签
            ta, tb = a, b
            for _ in range(depth):
                ta, tb = ta.parent, tb.parent
            return -1 if ta.pair < tb.pair else 1

        def prefer(cand, best):
            """cand 是否优于 best：先比代价三项，全并列才比匹配序号字典序。"""
            if cand[0] != best[0]:
                return cand[0] < best[0]
            if cand[1] != best[1]:
                return cand[1] < best[1]
            if cand[2] != best[2]:
                return cand[2] < best[2]
            return lex_cmp(cand[3], best[3]) < 0

        def append_better(pc, pair, best):
            """比较“父序列 pc + 新块 pair”与 best 的字典序（不实体化节点）。

            末端先比 pair，不同立即有结论；相同再沿父链复用 lex_cmp，
            从而每个网格单元只为最终胜出者创建一个 Trie 节点。
            """
            bp = best[3].pair
            if pair != bp:
                return pair < bp
            return lex_cmp(pc, best[3].parent) < 0

        cur = [None] * (m + 1)
        base = prev[0]  # j = 0：只能跳过稿件行
        cur[0] = (
            base[0] + li_len, base[1] + 1, base[2], base[3]
        )
        backtrack[i][0] = _BT_SKIP_LINE

        for j in range(1, m + 1):
            # 入边 1：跳过字幕 j-1
            c = cur[j - 1]
            best = (c[0] + sub_lens[j - 1], c[1] + 1, c[2], c[3])
            best_code = _BT_SKIP_SUB

            # 入边 2：跳过稿件行 i-1
            c = prev[j]
            cand = (c[0] + li_len, c[1] + 1, c[2], c[3])
            if prefer(cand, best):
                best, best_code = cand, _BT_SKIP_LINE

            # 入边 3：行 i-1 匹配连续字幕块 [j-k, j)
            max_k = min(line_max_k[i - 1], j)
            for k in range(1, max_k + 1):
                s = j - k
                joined = joins.get((s, k))
                if joined is None:
                    break  # 块跨缺口；更长的 k 同样跨缺口
                pc = prev[s]
                # margin = 候选总代价追平当前最优所允许的最大距离：
                # dist < margin 严格更优；dist == margin 同价但块匹配
                # 的 skips 不增加，仍可能凭后续裁决胜出；dist > margin
                # 严格更差，丢弃。故带状截断上界取 margin+1。
                margin = best[0] - pc[0]
                if margin < 0:
                    continue
                upper = margin + 1
                if abs(len(joined) - li_len) >= upper:
                    continue
                dist = _codepoint_edit_distance(li_text, joined, upper=upper)
                if dist > margin:
                    continue
                cost = pc[0] + dist
                joins_count = pc[2] + (k - 1)
                pair = (sub_seqs[s], sub_seqs[j - 1])
                # 先比代价三项；全并列才做虚拟字典序比较，胜出才实体化
                if (
                    cost < best[0]
                    or (
                        cost == best[0]
                        and (
                            pc[1] < best[1]
                            or (
                                pc[1] == best[1]
                                and (
                                    joins_count < best[2]
                                    or (
                                        joins_count == best[2]
                                        and append_better(pc[3], pair, best)
                                    )
                                )
                            )
                        )
                    )
                ):
                    best = (cost, pc[1], joins_count, pc[3].child(pair))
                    best_code = _BT_MATCH_BASE + k - 1

            cur[j] = best
            backtrack[i][j] = best_code
        prev = cur

    final = prev[m]
    return final[0], final[3], backtrack


def _codepoint_edit_distance(a: str, b: str, upper: "int | None" = None) -> int:
    """两个字符串的 Unicode 码点 Levenshtein 编辑距离（增删改各代价 1）。

    逐码点（Python str 元素）计算，不做任何归一化；空串距离为另一串
    码点数。

    upper 为调用方关心的截断上界：真实距离 < upper 时返回精确距离，
    否则返回 upper（这些候选在调用处都会被丢弃或仅需知道“不更优”）。
    优先使用 rapidfuzz 的 C 实现（score_cutoff=upper-1，其超过截断时
    恰好返回 cutoff+1=upper，语义一致）；无该依赖时回退到带状 DP。
    """
    if upper is None:
        return _rf_distance(a, b) if _HAS_RAPIDFUZZ else _banded_distance(a, b)
    if _HAS_RAPIDFUZZ:
        return _rf_distance(a, b, score_cutoff=upper - 1)
    return _banded_distance(a, b, upper)


try:  # 可选高性能依赖：rapidfuzz 提供 C 实现的逐码点 Levenshtein
    from rapidfuzz.distance import Levenshtein as _Levenshtein

    _HAS_RAPIDFUZZ = True

    def _rf_distance(a: str, b: str, score_cutoff: "int | None" = None) -> int:
        return _Levenshtein.distance(a, b, score_cutoff=score_cutoff)
except ImportError:  # pragma: no cover - 取决于部署镜像是否安装 rapidfuzz
    _HAS_RAPIDFUZZ = False


def _banded_distance(a: str, b: str, upper: "int | None" = None) -> int:
    """无 rapidfuzz 时的回退：带状滚动数组 Levenshtein，契约与主函数相同。

    只计算 |i-j| < upper 的带状区域（带外格点必 ≥ upper），终值达
    upper 即截断返回。
    """
    la, lb = len(a), len(b)
    if upper is None:
        upper = max(la, lb)
    if la == 0:
        return min(lb, upper)
    if lb == 0:
        return min(la, upper)
    if abs(la - lb) >= upper:
        return upper
    INF = upper + 1
    prev_row = [INF] * (lb + 1)
    cur_row = [INF] * (lb + 1)
    prev_row[0] = 0
    for j in range(1, min(lb, upper - 1) + 1):
        prev_row[j] = j
    for i in range(1, la + 1):
        cur_row[0] = i if i < upper else INF
        ca = a[i - 1]
        lo = max(1, i - (upper - 1))
        hi = min(lb, i + (upper - 1))
        if lo > 1:
            cur_row[lo - 1] = INF
        for j in range(lo, hi + 1):
            up_left = prev_row[j - 1] + (0 if ca == b[j - 1] else 1)
            up = prev_row[j] + 1
            left = cur_row[j - 1] + 1
            value = up_left
            if up < value:
                value = up
            if left < value:
                value = left
            cur_row[j] = value
        if hi < lb:
            cur_row[hi + 1] = INF
        prev_row, cur_row = cur_row, prev_row
    result = prev_row[lb]
    return upper if result >= upper else result


# ---------- 命名消费者拉取游标 ----------


def claim_batch(channel: str, consumer: str, limit: int, lease_seconds: float) -> dict:
    """领取连续批次：从已确认游标后返回至多 limit 条（含缺口标记）并签发租约。

    单个 BEGIN IMMEDIATE 事务内完成“注册消费者 -> 校验无未过期租约 ->
    截取连续批次 -> 写入新租约”，因此并发领取同一消费者时只有一个能
    拿到租约，其余得到 409 lease_active。租约过期后允许重新领取同一
    未确认批次（覆盖旧租约、签发新令牌）；无新内容时不签发租约。
    """
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO consumers (channel, consumer) VALUES (?, ?) "
            "ON CONFLICT(channel, consumer) DO NOTHING",
            (channel, consumer),
        )
        row = conn.execute(
            "SELECT cursor, lease_token, lease_from, lease_to, lease_expires_at "
            "FROM consumers WHERE channel = ? AND consumer = ?",
            (channel, consumer),
        ).fetchone()

        now = _now()
        if row["lease_token"] is not None and row["lease_expires_at"] > now:
            raise ConsumerError(
                "lease_active",
                f"消费者 {consumer} 持有未过期租约（批次 "
                f"{row['lease_from']}..{row['lease_to']}），到期前不得重复领取",
            )

        watermark = _watermark_of(conn, channel)
        cursor = row["cursor"]
        items = _fetch_finalized_items(conn, channel, cursor, limit)
        if not items:
            # 无新内容：返回空批次且不签发租约（过期租约留待下次领取覆盖，
            # 状态查询仍可见其 expired=true）
            return {
                "channel": channel,
                "consumer": consumer,
                "cursor": cursor,
                "watermark": watermark,
                "lease_token": None,
                "lease_expires_at": None,
                "items": [],
            }

        token = str(uuid.uuid4())
        expires_at = now + lease_seconds
        conn.execute(
            "UPDATE consumers SET lease_token = ?, lease_from = ?, lease_to = ?, "
            "lease_expires_at = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE channel = ? AND consumer = ?",
            (token, cursor + 1, items[-1]["seq"], expires_at, channel, consumer),
        )
        return {
            "channel": channel,
            "consumer": consumer,
            "cursor": cursor,
            "watermark": watermark,
            "lease_token": token,
            "lease_expires_at": _iso_utc(expires_at),
            "items": items,
        }


def ack_batch(
    channel: str,
    consumer: str,
    lease_token: str,
    expected_cursor: int,
    new_cursor: int,
) -> dict:
    """确认批次：校验租约与游标后把已确认游标推进到 new_cursor。

    全部校验先于写入且在单个 BEGIN IMMEDIATE 事务内完成：过期、旧令牌、
    越界或并发落后（expected_cursor 与当前游标不一致）的请求一律拒绝，
    不改写任何状态；成功时推进游标并清空租约，下一批从新游标后继续。
    """
    with write_tx() as conn:
        row = conn.execute(
            "SELECT cursor, lease_token, lease_from, lease_to, lease_expires_at "
            "FROM consumers WHERE channel = ? AND consumer = ?",
            (channel, consumer),
        ).fetchone()
        if row is None:
            raise ConsumerError(
                "no_active_lease",
                f"消费者 {consumer} 在频道 {channel} 从未领取批次，没有可确认的租约",
            )
        if expected_cursor != row["cursor"]:
            raise ConsumerError(
                "cursor_conflict",
                f"expected_cursor={expected_cursor} 与当前已确认游标 "
                f"{row['cursor']} 不一致（可能已有并发确认推进了游标）",
            )
        if row["lease_token"] is None:
            raise ConsumerError(
                "no_active_lease",
                f"消费者 {consumer} 当前没有待确认的租约",
            )
        if lease_token != row["lease_token"]:
            raise ConsumerError(
                "lease_token_mismatch",
                "lease_token 与当前租约不匹配（旧令牌或他人令牌）",
            )
        if row["lease_expires_at"] <= _now():
            raise ConsumerError(
                "lease_expired",
                "租约已过期，请重新领取批次后再确认",
            )
        if not row["lease_from"] <= new_cursor <= row["lease_to"]:
            raise ConsumerError(
                "cursor_out_of_range",
                f"new_cursor={new_cursor} 不在本批次 "
                f"{row['lease_from']}..{row['lease_to']} 范围内",
            )

        conn.execute(
            "UPDATE consumers SET cursor = ?, lease_token = NULL, lease_from = NULL, "
            "lease_to = NULL, lease_expires_at = NULL, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE channel = ? AND consumer = ?",
            (new_cursor, channel, consumer),
        )
        return {
            "channel": channel,
            "consumer": consumer,
            "previous_cursor": row["cursor"],
            "cursor": new_cursor,
            "watermark": _watermark_of(conn, channel),
        }


def get_consumer_status(channel: str, consumer: str) -> dict:
    """查询消费者状态：已确认游标、水位线、滞后量与当前租约（含是否已过期）。"""
    with read_tx() as conn:
        row = conn.execute(
            "SELECT cursor, lease_token, lease_from, lease_to, lease_expires_at "
            "FROM consumers WHERE channel = ? AND consumer = ?",
            (channel, consumer),
        ).fetchone()
        if row is None:
            raise ConsumerError(
                "consumer_not_found",
                f"消费者 {consumer} 在频道 {channel} 不存在（尚未领取过批次）",
                status_code=404,
            )

        watermark = _watermark_of(conn, channel)
        lease = None
        if row["lease_token"] is not None:
            lease = {
                "from_seq": row["lease_from"],
                "to_seq": row["lease_to"],
                "expires_at": _iso_utc(row["lease_expires_at"]),
                "expired": row["lease_expires_at"] <= _now(),
            }
        return {
            "channel": channel,
            "consumer": consumer,
            "cursor": row["cursor"],
            "watermark": watermark,
            "lag": watermark - row["cursor"],
            "lease": lease,
        }
