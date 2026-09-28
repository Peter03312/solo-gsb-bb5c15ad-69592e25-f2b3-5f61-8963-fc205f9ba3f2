from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class FragmentIn(BaseModel):
    seq: int = Field(..., ge=1, description="片段序号，从 1 开始连续编号")
    revision: int = Field(..., ge=0, description="修订号，同一序号只接受更大的值")
    text: str = Field(..., description="字幕文本")


class GapDecisionIn(BaseModel):
    start_seq: int = Field(..., ge=1, description="缺口起点序号，必须等于当前水位线下一位")
    end_seq: int = Field(..., ge=1, description="缺口终点序号（含），不得小于起点")

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "GapDecisionIn":
        if self.end_seq < self.start_seq:
            raise ValueError("end_seq 不得小于 start_seq")
        return self


class FinalizedItem(BaseModel):
    kind: str = Field("subtitle", description="条目类型：subtitle=字幕；gap=永久缺口标记")
    seq: int
    revision: Optional[int] = Field(None, description="修订号（仅字幕条目存在）")
    text: Optional[str] = Field(None, description="字幕文本（仅字幕条目存在）")
    start_seq: Optional[int] = Field(None, description="缺口区间起点（仅缺口条目存在）")
    end_seq: Optional[int] = Field(None, description="缺口区间终点（仅缺口条目存在）")


class FinalizedEffectiveItem(BaseModel):
    kind: str = Field("subtitle", description="条目类型：subtitle=字幕（已合并最新更正）；gap=永久缺口标记")
    seq: int
    revision: Optional[int] = Field(None, description="原始定稿修订号（仅字幕条目存在）")
    text: Optional[str] = Field(None, description="生效文本：original 视图为定稿原文，effective 视图为最新更正文本")
    start_seq: Optional[int] = Field(None, description="缺口区间起点（仅缺口条目存在）")
    end_seq: Optional[int] = Field(None, description="缺口区间终点（仅缺口条目存在）")
    correction_version: Optional[int] = Field(
        None,
        description=(
            "该序号的当前更正版本：字幕条目恒返回，0=未更正、>0=已合并的更正版本；"
            "缺口条目为 null。仅 effective 视图返回该字段"
        ),
    )


class SubmitResponse(BaseModel):
    channel: str
    seq: int
    applied: bool = Field(..., description="本次提交是否生效（被接受并写入缓冲区）")
    reason: Optional[str] = Field(
        None,
        description="未生效原因：stale_revision=修订号不够新；already_finalized=序号已定稿",
    )
    watermark: int = Field(..., description="提交处理完成后的最新水位线")
    finalized: List[FinalizedItem] = Field(
        default_factory=list, description="本次提交触发新定稿的片段（缺口补齐时可能一次释放多条）"
    )


class SkippedGap(BaseModel):
    start_seq: int
    end_seq: int


class GapDecisionResponse(BaseModel):
    channel: str
    applied: bool = Field(..., description="裁决是否生效；false 时不产生任何写入")
    idempotent: bool = Field(..., description="是否为重复提交已完整裁决区间的幂等返回")
    reason: Optional[str] = Field(
        None,
        description=(
            "未生效原因：start_not_at_watermark=起点不是水位线下一位；"
            "buffered_fragments=区间内存在已缓冲片段；"
            "already_finalized=区间已完全定稿；gap_overlap=与既有裁决部分重叠"
        ),
    )
    watermark: int = Field(..., description="裁决处理完成后的最新水位线")
    skipped: Optional[SkippedGap] = Field(None, description="本次（或原裁决）标记永久丢失的区间")
    finalized: List[FinalizedItem] = Field(
        default_factory=list, description="本次（或原裁决时）随裁决新定稿的连续片段"
    )


class FinalizedResponse(BaseModel):
    channel: str
    watermark: int
    items: List[FinalizedItem] = Field(
        ..., description="按序号升序混合返回字幕（kind=subtitle）与缺口标记（kind=gap）"
    )


class FinalizedEffectiveResponse(BaseModel):
    channel: str
    watermark: int
    view: Literal["effective"] = Field("effective", description="视图：effective=合并最新更正文本")
    correction_version: int = Field(
        ..., description="本页范围内出现过的最大更正版本（0 表示本页均无更正）"
    )
    items: List[FinalizedEffectiveItem] = Field(
        ..., description="按序号升序混合返回生效字幕（已合并最新更正）与缺口标记"
    )


# ---------- 追加式更正层 ----------


class CorrectionIn(BaseModel):
    expected_version: int = Field(
        ...,
        ge=0,
        description="客户端认为的当前更正版本：无更正时必须为 0，之后必须等于该序号最新更正版本",
    )
    text: str = Field(..., min_length=1, description="更正后的字幕文本")
    reason: str = Field(..., min_length=1, description="更正原因（随审计记录永久保留）")

    @field_validator("text", "reason")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不得为空白字符串")
        return v


class CorrectionResponse(BaseModel):
    channel: str
    seq: int = Field(..., description="被更正的定稿序号")
    correction_version: int = Field(..., description="本次追加后该序号的更正版本（首个更正为 1）")
    text: str = Field(..., description="本次更正文本（原始定稿不受影响，仍可经 view=original 查询）")
    reason: str
    created_at: str = Field(..., description="更正落库时间（UTC ISO8601）")


class CorrectionAuditItem(BaseModel):
    channel: str
    seq: int
    version: int = Field(..., description="更正版本，从 1 起递增")
    text: str = Field(..., description="该版本的更正文本")
    reason: str
    created_at: str


class CorrectionAuditResponse(BaseModel):
    channel: str
    seq: int
    current_version: int = Field(..., description="该序号当前更正版本（0 表示尚无更正）")
    items: List[CorrectionAuditItem] = Field(
        ..., description="该序号的全部更正记录，按 version 升序"
    )


# ---------- 批量更正 ----------


class BatchCorrectionItemIn(BaseModel):
    seq: int = Field(..., ge=1, description="被更正的定稿序号（必须已定稿且不是缺口）")
    expected_version: int = Field(
        ...,
        ge=0,
        description="客户端认为的当前更正版本：无更正时必须为 0，否则须等于该序号最新版本",
    )
    text: str = Field(..., min_length=1, description="更正后的字幕文本")
    reason: str = Field(..., min_length=1, description="更正原因（随审计记录永久保留）")

    @field_validator("text", "reason")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不得为空白字符串")
        return v


class BatchCorrectionIn(BaseModel):
    idempotency_key: str = Field(
        ...,
        min_length=1,
        description=(
            "幂等键（频道范围内唯一）：相同频道、相同键且请求完全相同时重试，"
            "始终返回原批次结果；键相同而内容不同返回 409 冲突"
        ),
    )
    corrections: List[BatchCorrectionItemIn] = Field(
        ..., min_length=1, max_length=100, description="本批更正项，按输入顺序处理（1~100 条）"
    )

    @field_validator("idempotency_key")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不得为空白字符串")
        return v


class BatchCorrectionResultItem(BaseModel):
    index: int = Field(..., description="该项在请求 corrections 数组中的下标（从 0 起，按输入顺序）")
    seq: int = Field(..., description="被更正的定稿序号")
    correction_version: int = Field(
        ..., description="本次追加后该序号的更正版本（首个更正为 1）"
    )
    text: str = Field(..., description="本次更正文本（原始定稿仍可经 view=original 查询）")
    reason: str
    created_at: str = Field(..., description="更正落库时间（UTC ISO8601）")


class BatchCorrectionResponse(BaseModel):
    batch_id: str = Field(..., description="批次号（UUID），同键完全相同请求重试返回同一批次号")
    channel: str
    idempotency_key: str
    idempotent: bool = Field(
        ...,
        description="false=本次新建批次；true=相同幂等键且完全相同请求重试，返回原批次结果",
    )
    created_at: str = Field(..., description="批次创建时间（UTC ISO8601）")
    results: List[BatchCorrectionResultItem] = Field(
        ..., description="逐项更正结果，顺序与请求 corrections 数组一致"
    )


class BatchCorrectionConflictItem(BaseModel):
    index: int = Field(..., description="该项在请求 corrections 数组中的下标（从 0 起，按输入顺序）")
    seq: int
    error: str = Field(
        ...,
        description=(
            "冲突原因：not_finalized=序号未定稿；seq_is_gap=序号为永久缺口；"
            "version_conflict=expected_version 与当前版本不一致；"
            "duplicate_seq=与本批更靠前的某项序号重复"
        ),
    )
    message: str


class BatchCorrectionErrorResponse(BaseModel):
    error: str = Field(
        ...,
        description=(
            "错误码：batch_conflict=批次内存在冲突项（整批拒绝、零写入），明细见 conflicts；"
            "idempotency_key_conflict=相同频道下该幂等键已被不同请求占用"
        ),
    )
    message: str
    conflicts: Optional[List[BatchCorrectionConflictItem]] = Field(
        None, description="error=batch_conflict 时的逐项冲突原因，按输入顺序返回"
    )


class ErrorResponse(BaseModel):
    error: str = Field(..., description="错误码")
    message: str


# ---------- 发布快照 ----------


class SnapshotCreateIn(BaseModel):
    idempotency_key: str = Field(
        ...,
        min_length=1,
        description=(
            "幂等键（频道范围内唯一）：同一 channel 使用相同 key 重试，"
            "始终返回同一快照，不会重复冻结"
        ),
    )

    @field_validator("idempotency_key")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不得为空白字符串")
        return v


class SnapshotResponse(BaseModel):
    snapshot_id: str = Field(..., description="快照 ID（UUID），用于分页拉取与更正历史查询")
    channel: str
    watermark: int = Field(..., description="创建时刻锁定的频道水位线")
    idempotency_key: str
    idempotent: bool = Field(
        ...,
        description="false=本次新建快照；true=相同 idempotency_key 重试，返回既有快照",
    )
    created_at: str = Field(..., description="快照创建时间（UTC ISO8601），即冻结时刻")


class SnapshotItem(BaseModel):
    kind: str = Field("subtitle", description="条目类型：subtitle=字幕；gap=永久缺口标记")
    seq: int
    revision: Optional[int] = Field(None, description="原始修订号（仅字幕条目存在）")
    text: Optional[str] = Field(
        None,
        description="快照冻结的原始定稿文本（仅字幕条目存在）",
    )
    start_seq: Optional[int] = Field(None, description="缺口区间起点（仅缺口条目存在）")
    end_seq: Optional[int] = Field(None, description="缺口区间终点（仅缺口条目存在）")


class SnapshotEffectiveItem(BaseModel):
    kind: str = Field("subtitle", description="条目类型：subtitle=字幕（快照时刻生效文本）；gap=永久缺口标记")
    seq: int
    revision: Optional[int] = Field(None, description="原始修订号（仅字幕条目存在）")
    text: Optional[str] = Field(
        None,
        description="快照时刻冻结的最新更正文本（仅字幕条目存在）",
    )
    start_seq: Optional[int] = Field(None, description="缺口区间起点（仅缺口条目存在）")
    end_seq: Optional[int] = Field(None, description="缺口区间终点（仅缺口条目存在）")
    correction_version: Optional[int] = Field(
        None,
        description=(
            "该序号在快照时刻的最新更正版本：字幕条目恒返回，0=创建时未更正；"
            "缺口条目为 null"
        ),
    )


class SnapshotItemsResponse(BaseModel):
    snapshot_id: str
    channel: str
    watermark: int = Field(..., description="快照锁定的水位线，也是条目总数")
    view: Literal["original"] = Field("original")
    items: List[SnapshotItem] = Field(
        ..., description="本页快照条目（kind=subtitle / gap），按 seq 升序"
    )


class SnapshotItemsEffectiveResponse(BaseModel):
    snapshot_id: str
    channel: str
    watermark: int
    view: Literal["effective"] = Field("effective")
    correction_version: int = Field(
        ..., description="本页范围内出现过的最大快照更正版本（0 表示本页均未更正）"
    )
    items: List[SnapshotEffectiveItem] = Field(
        ..., description="本页快照条目：字幕返回快照时刻冻结的生效文本，按 seq 升序"
    )


class SnapshotCorrectionItem(BaseModel):
    seq: int
    version: int = Field(..., description="更正版本，从 1 起递增")
    text: str = Field(..., description="该版本的更正文本（快照时刻冻结）")
    reason: str
    created_at: str


class SnapshotCorrectionsResponse(BaseModel):
    snapshot_id: str
    channel: str
    seq: int
    current_version: int = Field(
        ..., description="该序号在快照时刻的最新更正版本（0 表示快照时尚无更正）"
    )
    items: List[SnapshotCorrectionItem] = Field(
        ..., description="快照冻结的该序号全部更正记录，按 version 升序"
    )


# ---------- 快照差异 ----------


class SnapshotDiffEntry(BaseModel):
    change: Literal["added", "removed", "changed"] = Field(
        ...,
        description="差异类型：added=仅 other 快照存在；removed=仅基准快照存在；"
        "changed=两侧都有但冻结生效内容不同",
    )
    seq: int = Field(..., description="差异条目序号（两份快照按 seq 合并）")
    base: Optional[SnapshotEffectiveItem] = Field(
        None, description="基准快照（路径 snapshot_id）冻结的生效内容；added 时为 null"
    )
    other: Optional[SnapshotEffectiveItem] = Field(
        None,
        description="对方快照（other 参数）冻结的生效内容；removed 时为 null",
    )


class SnapshotDiffResponse(BaseModel):
    snapshot_id: str = Field(..., description="基准快照 ID（路径参数）")
    other_snapshot_id: str = Field(..., description="对方快照 ID（other 查询参数）")
    channel: str = Field(..., description="两个快照共同所属的频道")
    watermark: int = Field(..., description="基准快照锁定的水位线")
    other_watermark: int = Field(..., description="对方快照锁定的水位线")
    items: List[SnapshotDiffEntry] = Field(
        ...,
        description="本页差异条目，按 seq 升序；两侧完全一致的序号不返回。"
        "字幕以生效文本与 correction_version 判定，缺口以区间端点判定",
    )
    next_after: Optional[int] = Field(
        None,
        description="下一页游标：本页最后一条差异的 seq，以此作为 after 续拉；"
        "本页为空（无更多差异）时为 null",
    )


# ---------- 快照敏感词遮盖 ----------


class RedactionIn(BaseModel):
    words: List[str] = Field(
        ...,
        description=(
            "敏感词表：按精确相等去重（保持首次出现顺序）后须有 1~20 项，"
            "每项 1~128 个 Unicode 码点；匹配区分大小写、逐码点精确，不做归一化"
        ),
    )
    after: int = Field(
        0, ge=0, description="只返回序号大于该值的条目（字幕与缺口统一计数）"
    )
    limit: int = Field(100, ge=1, le=100, description="本页最多返回的条目数（1~100）")

    @field_validator("words")
    @classmethod
    def _dedup_and_validate_words(cls, v: List[str]) -> List[str]:
        deduped = list(dict.fromkeys(v))
        if not 1 <= len(deduped) <= 20:
            raise ValueError("敏感词表按精确相等去重后须有 1~20 项")
        for word in deduped:
            if not 1 <= len(word) <= 128:
                raise ValueError("每个敏感词须为 1~128 个 Unicode 码点")
        return deduped


class SnapshotRedactionResponse(BaseModel):
    snapshot_id: str
    channel: str
    watermark: int = Field(..., description="快照锁定的水位线，也是参与遮盖计算的条目总数")
    words: List[str] = Field(
        ...,
        description="实际生效的敏感词表（已按精确相等去重，保持请求中的首次出现顺序）",
    )
    items: List[SnapshotEffectiveItem] = Field(
        ...,
        description="本页遮盖结果（按 seq 升序）：字幕返回遮盖后的冻结生效文本，"
        "缺口标记原样保留",
    )
    next_after: Optional[int] = Field(
        None,
        description="下一页游标：还有后续条目时为本页末条序号，以其作为 after 续拉；"
        "否则为 null",
    )


# ---------- 剪辑字幕导出 ----------


class EditTimingIn(BaseModel):
    seq: int = Field(..., ge=1, description="字幕序号，须与快照内字幕 seq 一一对应")
    start_ms: int = Field(
        ..., ge=0, description="字幕在源时间轴上的起点（毫秒，非负整数，左闭）"
    )
    end_ms: int = Field(
        ..., ge=0, description="字幕在源时间轴上的终点（毫秒，非负整数，右开，须大于起点）"
    )

    @model_validator(mode="after")
    def _start_before_end(self) -> "EditTimingIn":
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms 必须小于 end_ms（左闭右开区间）")
        return self


class EditClipIn(BaseModel):
    start_ms: int = Field(
        ..., ge=0, description="剪辑片段在源时间轴上的起点（毫秒，非负整数，左闭）"
    )
    end_ms: int = Field(
        ..., ge=0, description="剪辑片段在源时间轴上的终点（毫秒，非负整数，右开，须大于起点）"
    )

    @model_validator(mode="after")
    def _start_before_end(self) -> "EditClipIn":
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms 必须小于 end_ms（左闭右开区间）")
        return self


class EditExportIn(BaseModel):
    timings: List[EditTimingIn] = Field(
        ...,
        description=(
            "全部字幕的源时间轴区间：须恰好覆盖快照内全部字幕 seq，"
            "无缺口、无重复、无多余序号（空快照须传空数组）"
        ),
    )
    clips: List[EditClipIn] = Field(
        ...,
        description="有序剪辑片段（可重排 / 重复 / 重叠），按输入顺序首尾拼接为成片时间轴",
    )


class EditCue(BaseModel):
    seq: int = Field(..., description="字幕序号（对应快照内字幕 seq）")
    clip_index: int = Field(
        ..., description="所属剪辑片段在请求 clips 数组中的下标（从 0 起，按输入顺序）"
    )
    text: str = Field(..., description="快照冻结的生效文本")
    start_ms: int = Field(..., description="cue 在成片时间轴上的起点（毫秒，左闭）")
    end_ms: int = Field(..., description="cue 在成片时间轴上的终点（毫秒，右开）")


class EditExportResponse(BaseModel):
    snapshot_id: str
    channel: str
    format: Literal["json"] = Field("json", description="响应格式：json=结构化 cue 列表")
    duration_ms: int = Field(
        ..., description="成片总时长（全部剪辑片段时长之和，毫秒）"
    )
    cues: List[EditCue] = Field(
        ..., description="成片字幕 cue，按 (start_ms, clip_index, seq) 升序"
    )


# ---------- 快照字幕引用迁移 ----------


class QuoteRange(BaseModel):
    """一段字幕文本范围：序号区间（含两端）+ 序号内码点偏移（左闭右开）。"""

    start_seq: int = Field(..., ge=1, description="起始字幕序号（含）")
    start_offset: int = Field(
        ..., ge=0, description="起始字幕内的起始 Unicode 码点偏移（含，从 0 起）"
    )
    end_seq: int = Field(..., ge=1, description="结束字幕序号（含）")
    end_offset: int = Field(
        ..., ge=0, description="结束字幕内的终点 Unicode 码点偏移（不含，允许等于文本码点长度）"
    )

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "QuoteRange":
        if self.end_seq < self.start_seq:
            raise ValueError("end_seq 不得小于 start_seq")
        if self.end_seq == self.start_seq and self.end_offset < self.start_offset:
            raise ValueError("同一字幕内 end_offset 不得小于 start_offset（左闭右开）")
        return self


class QuoteMigrationIn(BaseModel):
    target_snapshot_id: str = Field(
        ...,
        min_length=1,
        description="目标快照 ID（UUID）：引用将在该快照中重新定位，须与源快照同频道",
    )
    start_seq: int = Field(..., ge=1, description="源引用起始字幕序号（含）")
    start_offset: int = Field(
        ..., ge=0, description="源引用在起始字幕内的起始 Unicode 码点偏移（含，从 0 起）"
    )
    end_seq: int = Field(..., ge=1, description="源引用结束字幕序号（含），可与起始相同")
    end_offset: int = Field(
        ..., ge=0, description="源引用在结束字幕内的终点 Unicode 码点偏移（不含，允许等于文本码点长度）"
    )

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "QuoteMigrationIn":
        if self.end_seq < self.start_seq:
            raise ValueError("end_seq 不得小于 start_seq")
        if self.end_seq == self.start_seq and self.end_offset < self.start_offset:
            raise ValueError("同一字幕内 end_offset 不得小于 start_offset（左闭右开）")
        return self

    @field_validator("target_snapshot_id")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不得为空白字符串")
        return v


class QuoteMigrationResponse(BaseModel):
    source_snapshot_id: str = Field(..., description="源快照 ID（路径参数）")
    target_snapshot_id: str = Field(..., description="目标快照 ID（请求体）")
    channel: str = Field(..., description="两个快照共同所属的频道")
    status: Literal["found", "missing", "ambiguous"] = Field(
        ...,
        description=(
            "迁移结果：found=引用在目标快照中恰好命中一次；missing=零次命中；"
            "ambiguous=多次命中（不任选位置）"
        ),
    )
    source: QuoteRange = Field(..., description="请求给出的源引用序号 / 偏移（原样回显）")
    quote: str = Field(..., description="按源快照冻结生效文本提取出的非空引用（连续字幕无分隔拼接）")
    match: Optional[QuoteRange] = Field(
        None,
        description=(
            "status=found 时引用在目标快照中的起止序号 / 码点偏移（命中可跨相邻字幕）；"
            "missing / ambiguous 时为 null"
        ),
    )


# ---------- 稿件校稿对齐 ----------


class ScriptAlignmentLineIn(BaseModel):
    text: str = Field(..., min_length=1, description="稿件行文本（有序、非空，按 Unicode 码点计长）")
    max_subtitles: int = Field(
        ..., ge=1, le=3, description="本行最多可匹配合并的连续字幕数（1~3）"
    )

    @field_validator("text")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不得为空白字符串")
        return v


class ScriptAlignmentIn(BaseModel):
    lines: List[ScriptAlignmentLineIn] = Field(
        ..., min_length=1, max_length=100, description="有序稿件行（1~100 条，按输入顺序对齐）"
    )


class ScriptAlignmentItem(BaseModel):
    index: int = Field(..., description="稿件行在请求 lines 数组中的下标（从 0 起，按输入顺序）")
    matched: bool = Field(..., description="true=本行匹配了连续字幕；false=本行被跳过（未匹配）")
    start_seq: Optional[int] = Field(
        None, description="匹配字幕块的起始快照序号（含）；未匹配时为 null"
    )
    end_seq: Optional[int] = Field(
        None, description="匹配字幕块的结束快照序号（含）；未匹配时为 null"
    )
    subtitles: Optional[int] = Field(
        None, description="本行实际合并的字幕数（1~max_subtitles）；未匹配时为 null"
    )
    distance: Optional[int] = Field(
        None,
        description="稿件行与所匹配字幕块拼接文本的 Unicode 码点编辑距离；未匹配时为 null",
    )


class ScriptAlignmentResponse(BaseModel):
    snapshot_id: str
    channel: str
    watermark: int = Field(..., description="快照锁定的水位线（对齐只读取该快照冻结的字幕）")
    total_cost: int = Field(..., description="全局最优对齐的总代价（码点编辑距离与跳过码点数之和）")
    alignments: List[ScriptAlignmentItem] = Field(
        ..., description="逐行对齐结果，顺序与请求 lines 数组一致（未匹配行各序号字段为 null）"
    )
    unmatched_script_lines: List[int] = Field(
        ..., description="被跳过（未匹配）的稿件行下标列表（从 0 起，升序）"
    )
    unmatched_subtitle_seqs: List[int] = Field(
        ..., description="被跳过（未匹配）的字幕快照序号列表（升序）"
    )


# ---------- 命名消费者拉取游标 ----------


class ClaimIn(BaseModel):
    limit: int = Field(
        100, ge=1, le=10000, description="本批次最多返回的条目数（字幕与缺口标记统一计数）"
    )
    lease_seconds: float = Field(
        30.0, gt=0, le=3600, description="租约有效期（秒）；过期后同一未确认批次可被重新领取"
    )


class ClaimResponse(BaseModel):
    channel: str
    consumer: str
    cursor: int = Field(..., description="领取时的已确认游标；本批次从 cursor+1 开始")
    watermark: int = Field(..., description="领取时的频道水位线")
    lease_token: Optional[str] = Field(
        None, description="租约令牌，确认批次时回传；无新内容时为 null"
    )
    lease_expires_at: Optional[str] = Field(
        None, description="租约过期时刻（UTC ISO8601）；无租约时为 null"
    )
    items: List[FinalizedItem] = Field(
        default_factory=list,
        description="已确认游标后的连续批次（字幕与缺口标记按 seq 升序混合）",
    )


class AckIn(BaseModel):
    lease_token: str = Field(..., min_length=1, description="领取时签发的租约令牌")
    expected_cursor: int = Field(
        ..., ge=0, description="客户端认为的当前已确认游标（乐观并发控制）"
    )
    new_cursor: int = Field(
        ..., ge=1, description="要推进到的序号，必须落在本批次序号范围内"
    )


class AckResponse(BaseModel):
    channel: str
    consumer: str
    previous_cursor: int = Field(..., description="确认前的已确认游标")
    cursor: int = Field(..., description="确认后的已确认游标（= new_cursor）")
    watermark: int = Field(..., description="确认时的频道水位线")


class LeaseStatus(BaseModel):
    from_seq: int = Field(..., description="租约批次起点序号（含）")
    to_seq: int = Field(..., description="租约批次终点序号（含）")
    expires_at: str = Field(..., description="租约过期时刻（UTC ISO8601）")
    expired: bool = Field(..., description="查询时刻租约是否已过期")


class ConsumerStatusResponse(BaseModel):
    channel: str
    consumer: str
    cursor: int = Field(..., description="已确认游标（初始 0）")
    watermark: int = Field(..., description="频道当前水位线")
    lag: int = Field(..., description="未确认条目数（watermark - cursor）")
    lease: Optional[LeaseStatus] = Field(None, description="当前租约；无租约时为 null")
