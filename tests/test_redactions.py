"""快照敏感词遮盖测试。

覆盖：
- 基于快照冻结生效文本的遮盖（含快照时刻的更正文本），快照后的新
  更正 / 新定稿不影响遮盖结果；
- 相邻字幕按序号无分隔拼接：跨字幕词语完整命中；缺口阻断拼接：
  跨缺口词语绝不命中；
- 所有词的重叠命中取并集，每个被覆盖码点替换为一个 *（长度不变）；
- 区分大小写、逐码点精确匹配，不做归一化；
- 词表去重（1~20 项、每项 1~128 码点）与 after/limit 的 422 校验，
  快照不存在 404；
- 先按完整快照计算遮盖再分页：跨页词语完整遮盖，next_after 在还有
  后续条目时指向本页末序号，否则为 null；
- 只读：快照内容与当前定稿 / 更正均不被修改。
"""


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


def redact(client, snapshot_id, words, **params):
    payload = {"words": words}
    payload.update(params)
    return client.post(f"/snapshots/{snapshot_id}/redactions", json=payload)


# ---------- 基本遮盖语义 ----------


def test_redaction_masks_within_and_across_adjacent_subtitles(client):
    submit(client, "c1", 1, 1, "abcdef")
    submit(client, "c1", 2, 1, "ghijkl")
    snap = make_snapshot(client, "c1", "k1")

    # 相邻字幕无分隔拼接为 "abcdefghijkl"：跨字幕的 "defghi" 完整命中
    body = redact(client, snap["snapshot_id"], ["defghi"]).json()
    assert body["snapshot_id"] == snap["snapshot_id"]
    assert body["channel"] == "c1"
    assert body["watermark"] == 2
    assert body["words"] == ["defghi"]
    assert body["next_after"] is None
    assert [(i["kind"], i["seq"], i["text"]) for i in body["items"]] == [
        ("subtitle", 1, "abc***"),
        ("subtitle", 2, "***jkl"),
    ]
    assert body["items"][0]["correction_version"] == 0
    assert body["items"][0]["revision"] == 1


def test_redaction_uses_frozen_effective_text(client):
    submit(client, "c1", 1, 1, "原始文本")
    correct(client, "c1", 1, 0, "更正文本")
    snap = make_snapshot(client, "c1", "k1")

    # 快照后再更正、再定稿，均不影响既有快照的遮盖
    correct(client, "c1", 1, 1, "快照后再改")
    submit(client, "c1", 2, 1, "第二条")

    body = redact(client, snap["snapshot_id"], ["更正", "原始"]).json()
    # 命中冻结的生效文本“更正文本”，而不是原始定稿或快照后的新更正
    assert [i["text"] for i in body["items"]] == ["**文本"]
    assert body["items"][0]["correction_version"] == 1
    assert body["watermark"] == 1


def test_redaction_gap_blocks_matching(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 3, 1, "def")
    decide_gap(client, "c1", 2, 2)
    snap = make_snapshot(client, "c1", "k1")

    # 缺口阻断拼接：跨缺口的 "abcdef" 不命中；段内的 "abc" 正常命中
    body = redact(client, snap["snapshot_id"], ["abcdef"]).json()
    assert [i["text"] for i in body["items"]] == ["abc", None, "def"]

    body = redact(client, snap["snapshot_id"], ["abc"]).json()
    assert [(i["kind"], i["seq"], i["text"]) for i in body["items"]] == [
        ("subtitle", 1, "***"),
        ("gap", 2, None),
        ("subtitle", 3, "def"),
    ]
    gap = body["items"][1]
    assert gap["start_seq"] == 2
    assert gap["end_seq"] == 2
    assert gap["revision"] is None
    assert gap["correction_version"] is None


def test_redaction_overlapping_hits_union(client):
    submit(client, "c1", 1, 1, "aaaa")
    snap1 = make_snapshot(client, "c1", "k1")
    submit(client, "c2", 1, 1, "abab")
    snap2 = make_snapshot(client, "c2", "k1")

    # "aa" 在 "aaaa" 中的重叠命中（0、1、2 位）取并集 -> 全部遮盖
    body = redact(client, snap1["snapshot_id"], ["aa"]).json()
    assert body["items"][0]["text"] == "****"

    # 多词命中区间取并集："ab" 覆盖 0-1 与 2-3，"ba" 覆盖 1-2 -> 全部遮盖
    body = redact(client, snap2["snapshot_id"], ["ab", "ba"]).json()
    assert body["items"][0]["text"] == "****"
    # 单词 "ab" 在 "abab" 中命中 0-1 与 2-3 -> 全部遮盖
    body = redact(client, snap2["snapshot_id"], ["ab"]).json()
    assert body["items"][0]["text"] == "****"
    # 单词 "ba" 只覆盖 1-2 -> 首尾保留
    body = redact(client, snap2["snapshot_id"], ["ba"]).json()
    assert body["items"][0]["text"] == "a**b"


def test_redaction_case_sensitive_and_code_point_exact(client):
    submit(client, "c1", 1, 1, "Abab")
    # "é"（U+00E9，单一码点）与 "é"（U+0065 U+0301，两个码点）不做归一化
    submit(client, "c1", 2, 1, "é")
    snap = make_snapshot(client, "c1", "k1")

    body = redact(client, snap["snapshot_id"], ["ab"]).json()
    # 区分大小写：首位的 "Ab" 不命中，只有 2-3 位的 "ab" 被遮盖
    assert body["items"][0]["text"] == "Ab**"

    body = redact(client, snap["snapshot_id"], ["é"]).json()
    assert body["items"][1]["text"] == "é"  # 分解形式不命中，原样保留
    body = redact(client, snap["snapshot_id"], ["e"]).json()
    assert body["items"][1]["text"] == "*́"  # 逐码点：只有 e 被替换


def test_redaction_each_covered_code_point_becomes_one_star(client):
    submit(client, "c1", 1, 1, "敏感词测试敏感词")
    snap = make_snapshot(client, "c1", "k1")

    body = redact(client, snap["snapshot_id"], ["敏感词"]).json()
    text = body["items"][0]["text"]
    assert text == "***测试***"
    assert len(text) == len("敏感词测试敏感词")  # 每个被覆盖码点恰好一个 *


def test_redaction_words_deduped_preserving_first_occurrence_order(client):
    submit(client, "c1", 1, 1, "abcabc")
    snap = make_snapshot(client, "c1", "k1")

    body = redact(client, snap["snapshot_id"], ["abc", "b", "abc", "b"]).json()
    assert body["words"] == ["abc", "b"]
    assert body["items"][0]["text"] == "******"


# ---------- 分页：先全量遮盖，再截取 ----------


def test_redaction_pagination_computes_on_full_snapshot_first(client):
    submit(client, "c1", 1, 1, "head")
    submit(client, "c1", 2, 1, "ab")
    submit(client, "c1", 3, 1, "cd")
    submit(client, "c1", 4, 1, "tail")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # "abcd" 横跨 seq2/seq3 的页边界：两页都完整遮盖
    page1 = redact(client, sid, ["abcd"], limit=2).json()
    assert [(i["seq"], i["text"]) for i in page1["items"]] == [
        (1, "head"),
        (2, "**"),
    ]
    assert page1["next_after"] == 2

    page2 = redact(client, sid, ["abcd"], after=page1["next_after"], limit=2).json()
    assert [(i["seq"], i["text"]) for i in page2["items"]] == [
        (3, "**"),
        (4, "tail"),
    ]
    assert page2["next_after"] is None  # 已到末页

    # after 越过水位线：空页，next_after 为 null
    body = redact(client, sid, ["abcd"], after=99).json()
    assert body["items"] == []
    assert body["next_after"] is None


def test_redaction_pagination_counts_gaps_and_defaults(client):
    submit(client, "c1", 1, 1, "one")
    submit(client, "c1", 3, 1, "three")
    decide_gap(client, "c1", 2, 2)
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # 默认 after=0、limit=100：字幕与缺口统一计数，一页拉全
    body = redact(client, sid, ["zzz"]).json()
    assert [(i["kind"], i["seq"]) for i in body["items"]] == [
        ("subtitle", 1),
        ("gap", 2),
        ("subtitle", 3),
    ]
    assert body["next_after"] is None

    # limit=2：缺口也占额度，还有后续时 next_after 为本页末序号
    body = redact(client, sid, ["zzz"], limit=2).json()
    assert [(i["kind"], i["seq"]) for i in body["items"]] == [
        ("subtitle", 1),
        ("gap", 2),
    ]
    assert body["next_after"] == 2


def test_redaction_empty_snapshot(client):
    snap = make_snapshot(client, "ghost", "k1")
    body = redact(client, snap["snapshot_id"], ["词"]).json()
    assert body["watermark"] == 0
    assert body["items"] == []
    assert body["next_after"] is None
    assert body["words"] == ["词"]


# ---------- 只读：不修改快照或当前字幕 ----------


def test_redaction_does_not_modify_snapshot_or_live_state(client):
    submit(client, "c1", 1, 1, "敏感词甲")
    correct(client, "c1", 1, 0, "敏感词乙")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    body = redact(client, sid, ["敏感词"]).json()
    assert body["items"][0]["text"] == "***乙"

    # 快照两个视图与快照内审计均保持原样
    original = client.get(f"/snapshots/{sid}/items").json()
    assert original["items"][0]["text"] == "敏感词甲"
    effective = client.get(
        f"/snapshots/{sid}/items", params={"view": "effective"}
    ).json()
    assert effective["items"][0]["text"] == "敏感词乙"
    assert effective["items"][0]["correction_version"] == 1
    audit = client.get(f"/snapshots/{sid}/items/1/corrections").json()
    assert audit["current_version"] == 1

    # 当前定稿与更正链不受影响
    live = client.get("/channels/c1/finalized", params={"view": "effective"}).json()
    assert live["items"][0]["text"] == "敏感词乙"
    live_audit = client.get("/channels/c1/finalized/1/corrections").json()
    assert live_audit["current_version"] == 1

    # 重复遮盖结果稳定（幂等的纯计算）
    again = redact(client, sid, ["敏感词"]).json()
    assert again == body


# ---------- 参数校验与错误路径 ----------


def test_redaction_word_list_validation(client):
    submit(client, "c1", 1, 1, "abc")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # 缺 words、空表、去重后超 20 项
    assert client.post(f"/snapshots/{sid}/redactions", json={}).status_code == 422
    assert redact(client, sid, []).status_code == 422
    assert redact(client, sid, [f"w{i}" for i in range(21)]).status_code == 422
    # 空词、超长词（129 码点）
    assert redact(client, sid, [""]).status_code == 422
    assert redact(client, sid, ["x" * 129]).status_code == 422
    # words 不是数组
    assert client.post(
        f"/snapshots/{sid}/redactions", json={"words": "abc"}
    ).status_code == 422

    # 边界合法：去重后恰好 20 项、词长恰好 128 码点
    assert redact(client, sid, [f"w{i}" for i in range(20)]).status_code == 200
    assert redact(client, sid, ["x" * 128]).status_code == 200
    # 原始列表可超 20 项：去重后 ≤20 即合法
    assert redact(client, sid, ["a", "b", "c"] * 10).status_code == 200


def test_redaction_after_and_limit_validation(client):
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    assert redact(client, sid, ["a"], after=-1).status_code == 422
    assert redact(client, sid, ["a"], limit=0).status_code == 422
    assert redact(client, sid, ["a"], limit=101).status_code == 422
    assert redact(client, sid, ["a"], after=0, limit=1).status_code == 200
    assert redact(client, sid, ["a"], after=0, limit=100).status_code == 200


def test_redaction_unknown_snapshot_returns_404(client):
    resp = redact(client, "does-not-exist", ["词"])
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"
