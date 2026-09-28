"""校稿对齐（POST /snapshots/{snapshot_id}/script-alignments）测试。

覆盖：
- 基于快照冻结生效文本对齐（含快照时刻的更正文本），快照后的新更正 /
  新定稿不影响对齐结果；
- 每行可匹配连续 1~max 条字幕（按序号无分隔拼接），不得跨缺口、不得
  复用字幕；稿件行 / 字幕均可跳过，跳过代价为码点数；
- 匹配代价为 Unicode 码点 Levenshtein 编辑距离（含 4 字节 emoji）；
- 动态规划全局最小总代价；总代价相同时依次取跳过项更少、合并字幕更少、
  匹配序号字典序更小的方案（含同字幕序号对、不同稿件行时优先更靠前行）；
- max_merged_subtitles 取 1~3 时的合并上限行为；
- 参数非法 422（条数越界、空白行、合并数越界、缺字段）、快照不存在 404；
- 只读：不修改快照或当前定稿 / 更正。
"""

import pytest

from app.service import _align_script_texts


def submit(client, channel, seq, revision, text="t"):
    resp = client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )
    assert resp.status_code == 200
    return resp.json()


def decide_gap(client, channel, start_seq, end_seq):
    resp = client.post(
        f"/channels/{channel}/gaps",
        json={"start_seq": start_seq, "end_seq": end_seq},
    )
    assert resp.status_code == 200
    return resp.json()


def correct(client, channel, seq, expected_version, text, reason="r"):
    resp = client.post(
        f"/channels/{channel}/finalized/{seq}/corrections",
        json={
            "expected_version": expected_version,
            "text": text,
            "reason": reason,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def make_snapshot(client, channel, key):
    resp = client.post(
        f"/channels/{channel}/snapshots", json={"idempotency_key": key}
    )
    assert resp.status_code == 200
    return resp.json()


def align(client, snapshot_id, lines, max_merged=3):
    return client.post(
        f"/snapshots/{snapshot_id}/script-alignments",
        json={"lines": lines, "max_merged_subtitles": max_merged},
    )


def alignments_by_line(body):
    return {item["line_index"]: item for item in body["alignments"]}


# ---------- 基本匹配 / 距离 / 只读 ----------


def test_exact_one_to_one_alignment(client):
    submit(client, "c1", 1, 1, "各位观众")
    submit(client, "c1", 2, 1, "晚上好")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], ["各位观众", "晚上好"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 0
    assert body["unmatched_lines"] == []
    assert body["unmatched_subtitle_seqs"] == []
    by_line = alignments_by_line(body)
    assert by_line[0] == {
        "line_index": 0, "start_seq": 1, "end_seq": 1, "distance": 0
    }
    assert by_line[1] == {
        "line_index": 1, "start_seq": 2, "end_seq": 2, "distance": 0
    }


def test_distance_is_unicode_codepoint_levenshtein(client):
    # "晚上好" vs "早上好"：替换 1 个码点；emoji 增补为 1 个码点
    submit(client, "c1", 1, 1, "晚上好")
    submit(client, "c1", 2, 1, "😀")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], ["早上好", "😀x"], 1)
    assert resp.status_code == 200
    by_line = alignments_by_line(resp.json())
    assert by_line[0]["distance"] == 1
    assert by_line[1]["distance"] == 1
    assert resp.json()["total_cost"] == 2


def test_alignment_uses_frozen_effective_text(client):
    submit(client, "c1", 1, 1, "原文")
    snap = make_snapshot(client, "c1", "s1")
    # 快照之后的更正对既有快照不可见
    correct(client, "c1", 1, 0, "快照后的更正")

    resp = align(client, snap["snapshot_id"], ["原文"], 1)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 0
    assert body["alignments"][0]["distance"] == 0


def test_alignment_is_read_only(client):
    submit(client, "c1", 1, 1, "abc")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], ["xyz"], 1)
    assert resp.status_code == 200
    # 快照内容原样可读
    items = client.get(f"/snapshots/{snap['snapshot_id']}/items").json()
    assert items["items"][0]["text"] == "abc"


# ---------- 合并字幕 ----------


def test_merge_up_to_max_adjacent_subtitles(client):
    submit(client, "c1", 1, 1, "你好")
    submit(client, "c1", 2, 1, "世界")
    submit(client, "c1", 3, 1, "！")
    snap = make_snapshot(client, "c1", "s1")

    # 三行合一：max=3 允许，拼接后精确相等
    resp = align(client, snap["snapshot_id"], ["你好世界！"], 3)
    body = resp.json()
    assert body["total_cost"] == 0
    assert body["alignments"] == [
        {"line_index": 0, "start_seq": 1, "end_seq": 3, "distance": 0}
    ]

    # max=2 时最多合并两条：seq1..2 合并后与稿件距离 1，seq3 跳过代价 1
    resp = align(client, snap["snapshot_id"], ["你好世界！"], 2)
    body = resp.json()
    assert body["total_cost"] == 2
    assert body["alignments"] == [
        {"line_index": 0, "start_seq": 1, "end_seq": 2, "distance": 1}
    ]
    assert body["unmatched_subtitle_seqs"] == [3]

    # max=1 不允许合并
    resp = align(client, snap["snapshot_id"], ["你好世界！"], 1)
    body = resp.json()
    match = body["alignments"][0]
    assert match["start_seq"] == match["end_seq"]
    # 其余两条字幕跳过
    assert sorted(body["unmatched_subtitle_seqs"]) == [
        s for s in (1, 2, 3) if s != match["start_seq"]
    ]


def test_merge_cannot_cross_gap(client):
    submit(client, "c1", 1, 1, "你好")
    decide_gap(client, "c1", 2, 2)
    submit(client, "c1", 3, 1, "世界")
    snap = make_snapshot(client, "c1", "snap-after-gap")

    # seq1 与 seq3 中间有缺口：即便 max_merged=3 也不能合并
    resp = align(client, snap["snapshot_id"], ["你好世界"], 3)
    assert resp.status_code == 200
    body = resp.json()
    # 两种单条方案代价相同（4），字典序取 seq1；seq3 整条按码点跳过
    assert body["alignments"] == [
        {"line_index": 0, "start_seq": 1, "end_seq": 1, "distance": 2}
    ]
    assert body["total_cost"] == 4
    assert body["unmatched_subtitle_seqs"] == [3]
    # 缺口序号 2 不作为“未匹配字幕”出现
    assert 2 not in body["unmatched_subtitle_seqs"]


def test_merged_text_is_concatenated_without_separator(client):
    submit(client, "c1", 1, 1, "ab")
    submit(client, "c1", 2, 1, "cd")
    snap = make_snapshot(client, "c1", "s1")
    resp = align(client, snap["snapshot_id"], ["abcd"], 2)
    assert resp.json()["total_cost"] == 0


# ---------- 跳过稿件行 / 字幕 ----------


def test_skip_lines_when_script_longer(client):
    submit(client, "c1", 1, 1, "你好")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], ["你好", "废话"], 1)
    body = resp.json()
    # 第二行无字幕可配 -> 整行跳过，代价为码点数 2
    assert body["total_cost"] == 2
    assert body["unmatched_lines"] == [1]
    assert body["unmatched_subtitle_seqs"] == []


def test_skip_subtitles_when_snapshot_longer(client):
    submit(client, "c1", 1, 1, "你好")
    submit(client, "c1", 2, 1, "再见")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], ["你好"], 1)
    body = resp.json()
    assert body["total_cost"] == 2  # seq2 文本码点数
    assert body["unmatched_subtitle_seqs"] == [2]
    assert body["unmatched_lines"] == []


def test_empty_snapshot_skips_all_lines(client):
    snap = make_snapshot(client, "c1", "empty")
    resp = align(client, snap["snapshot_id"], ["你好", "x"], 3)
    body = resp.json()
    assert body["watermark"] == 0
    assert body["total_cost"] == 3
    assert body["unmatched_lines"] == [0, 1]
    assert body["unmatched_subtitle_seqs"] == []
    assert body["alignments"] == []


def test_ordering_prevents_subtitle_reuse(client):
    # 两条相同稿件、两条相同字幕：必须一一对应，不能把同一字幕配两行
    submit(client, "c1", 1, 1, "ab")
    submit(client, "c1", 2, 1, "ab")
    snap = make_snapshot(client, "c1", "s1")
    body = align(client, snap["snapshot_id"], ["ab", "ab"], 3).json()
    assert body["total_cost"] == 0
    by_line = alignments_by_line(body)
    assert by_line[0]["start_seq"] == 1 and by_line[1]["start_seq"] == 2


# ---------- 打平规则 ----------


def test_tiebreak_fewer_skips_preferred():
    # 合并方案（仅跳过 1 条字幕）与“匹配 1 条 + 跳过 1 条字幕”代价相同，
    # 取跳过项更少的合并方案
    result = _align_script_texts(
        ["abc"], [1, 2, 3], ["ab", "bc", "abc"], 2
    )
    assert result["total_cost"] == 4
    match = result["alignments"][0]
    assert (match["start_seq"], match["end_seq"]) == (1, 2)
    assert result["unmatched_subtitle_seqs"] == [3]


def test_tiebreak_lexicographic_match_sequences():
    # 分组 (1)+(2,3) 与 (1,2)+(3) 代价 / 跳过 / 合并数全同：
    # 匹配序号序列字典序 (1,1,2,3) < (1,2,3,3)，取前者
    result = _align_script_texts(
        ["ab", "ab"], [1, 2, 3], ["ab", "ab", "ab"], 2
    )
    assert result["total_cost"] == 2
    by_line = {a["line_index"]: a for a in result["alignments"]}
    assert (by_line[0]["start_seq"], by_line[0]["end_seq"]) == (1, 1)
    assert (by_line[1]["start_seq"], by_line[1]["end_seq"]) == (2, 3)


def test_tiebreak_earlier_line_matches_when_pairs_identical():
    # 4 条稿件、3 条字幕时必有一行被跳过；当“匹配哪些字幕序号”相同、
    # 只是由哪一行去匹配存在多种等价方案时，字典序优先让最靠前的稿件行
    # 匹配（跳过更靠后的行）。
    # 全局最优（总代价 5、跳 1 行 0 字幕）：行0->seq2(2)、行1->seq3(1)、
    # 行2->seq6(1)，行3("x") 跳过；次优才是跳过行0 由行1..3 匹配。
    result = _align_script_texts(
        ["bc", "c", "bc", "x"], [2, 3, 6], ["你", "b", "c"], 3
    )
    assert result["total_cost"] == 5
    by_line = {a["line_index"]: a for a in result["alignments"]}
    assert (by_line[0]["start_seq"], by_line[0]["distance"]) == (2, 2)
    assert (by_line[1]["start_seq"], by_line[1]["distance"]) == (3, 1)
    assert (by_line[2]["start_seq"], by_line[2]["distance"]) == (6, 1)
    assert result["unmatched_lines"] == [3]
    assert result["unmatched_subtitle_seqs"] == []


# ---------- 纯 DP 单元测试：空输入 / 合并数 ----------


def test_align_no_lines_skips_all_subtitles():
    result = _align_script_texts([], [1, 2], ["a", "bb"], 1)
    assert result["total_cost"] == 3
    assert result["unmatched_subtitle_seqs"] == [1, 2]
    assert result["alignments"] == []


def test_align_respects_group_continuity_in_dp():
    # 字幕序号 1 与 3（缺口 2）：k=2 不允许把两者合并
    result = _align_script_texts(["ab"], [1, 3], ["a", "b"], 2)
    # 合并不可行：只能单条匹配 + 跳过另一条
    for match in result["alignments"]:
        assert match["start_seq"] == match["end_seq"]


# ---------- 404 / 422 ----------


def test_snapshot_not_found_returns_404(client):
    resp = align(client, "00000000-0000-0000-0000-000000000000", ["x"], 1)
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"


@pytest.mark.parametrize(
    "payload",
    [
        {"lines": [], "max_merged_subtitles": 1},
        {"lines": ["行"], "max_merged_subtitles": 0},
        {"lines": ["行"], "max_merged_subtitles": 4},
        {"max_merged_subtitles": 1},
        {"lines": ["行"]},
    ],
)
def test_invalid_payload_returns_422(client, payload):
    snap = make_snapshot(client, "c1", "s1")
    resp = client.post(
        f"/snapshots/{snap['snapshot_id']}/script-alignments", json=payload
    )
    assert resp.status_code == 422


def test_blank_lines_rejected_422(client):
    snap = make_snapshot(client, "c1", "s1")
    resp = client.post(
        f"/snapshots/{snap['snapshot_id']}/script-alignments",
        json={"lines": ["有效行", "   "], "max_merged_subtitles": 1},
    )
    assert resp.status_code == 422


def test_lines_stripped_before_alignment(client):
    submit(client, "c1", 1, 1, "正文")
    snap = make_snapshot(client, "c1", "s1")
    # 行首尾空白由 schema 去除，去除后与字幕精确相等
    resp = align(client, snap["snapshot_id"], ["  正文\t"], 1)
    assert resp.status_code == 200
    assert resp.json()["total_cost"] == 0


def test_more_than_100_lines_rejected_422(client):
    snap = make_snapshot(client, "c1", "s1")
    resp = client.post(
        f"/snapshots/{snap['snapshot_id']}/script-alignments",
        json={"lines": ["行"] * 101, "max_merged_subtitles": 1},
    )
    assert resp.status_code == 422


def test_100_lines_accepted(client):
    for seq in range(1, 101):
        submit(client, "c1", seq, 1, f"字{seq}")
    snap = make_snapshot(client, "c1", "s1")
    resp = align(client, snap["snapshot_id"], [f"字{seq}" for seq in range(1, 101)], 1)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 0
    assert len(body["alignments"]) == 100
