"""快照字幕引用迁移测试。

覆盖：
- 按源快照冻结生效文本提取引用（含快照时刻的更正文本），引用可跨
  相邻字幕；中间为空字幕时仍可跨越；
- 源引用不得越过缺口 / 空洞、不得越出快照水位线，码点偏移（左闭右开，
  按 Unicode 码点而非字节）越界、引用为空均 422 invalid_quote_range；
- 仅允许同频道快照：任一快照不存在 404，频道不一致 409；
- 在目标快照连续字幕文本中查找完整引用（命中可跨字幕，缺口阻断）：
  恰好一次 found、零次 missing、多次 ambiguous（不任选位置）；
- 迁移到内容已变化（更正 / 新增 / 缺口）的目标快照仍按冻结文本定位；
- 只读：不修改任何快照或当前定稿 / 更正。
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


def migrate(client, source_id, target_id, start_seq, start_offset, end_seq, end_offset):
    return client.post(
        f"/snapshots/{source_id}/quote-migrations",
        json={
            "target_snapshot_id": target_id,
            "start_seq": start_seq,
            "start_offset": start_offset,
            "end_seq": end_seq,
            "end_offset": end_offset,
        },
    )


# ---------- 基本命中 / 跨字幕 ----------


def test_migrate_quote_found_within_single_subtitle(client):
    submit(client, "c1", 1, 1, "hello world")
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 6, 1, 11)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "found"
    assert body["quote"] == "world"
    assert body["channel"] == "c1"
    assert body["source"] == {
        "start_seq": 1, "start_offset": 6, "end_seq": 1, "end_offset": 11,
    }
    assert body["match"] == {
        "start_seq": 1, "start_offset": 6, "end_seq": 1, "end_offset": 11,
    }


def test_migrate_quote_found_across_adjacent_subtitles(client):
    submit(client, "c1", 1, 1, "abcdef")
    submit(client, "c1", 2, 1, "ghijkl")
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    # 跨字幕引用 "defghi"（seq1 的后 3 码点 + seq2 的前 3 码点）
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 3, 2, 3)
    body = resp.json()
    assert body["status"] == "found"
    assert body["quote"] == "defghi"
    assert body["match"] == {
        "start_seq": 1, "start_offset": 3, "end_seq": 2, "end_offset": 3,
    }


def test_migrate_quote_found_when_target_text_shifted(client):
    # 目标快照文本已被更正、序号位置整体后移：引用仍应按内容精确定位
    submit(client, "c1", 1, 1, "旧的开头")
    src = make_snapshot(client, "c1", "s1")

    correct(client, "c1", 1, 0, "新的开头旧的开头结尾")
    tgt = make_snapshot(client, "c1", "t1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 4)
    body = resp.json()
    assert body["quote"] == "旧的开头"
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 1, "start_offset": 4, "end_seq": 1, "end_offset": 8,
    }


def test_migrate_quote_found_across_different_subtitle_boundary(client):
    # 同一引用在两个快照中的跨字幕切分点不同
    submit(client, "c1", 1, 1, "hello ")
    submit(client, "c1", 2, 1, "world!")
    src = make_snapshot(client, "c1", "s1")

    correct(client, "c1", 1, 0, "hello wor")
    correct(client, "c1", 2, 0, "ld!!")
    tgt = make_snapshot(client, "c1", "t1")

    # 源：跨 seq1/2 的 "lo world"（seq1[3:6] + seq2[0:5]）
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 3, 2, 5)
    body = resp.json()
    assert body["quote"] == "lo world"
    assert body["status"] == "found"
    # 目标拼接文本 "hello world!!" 中 "lo world" 位于 3..11：
    # seq1="hello wor"（9 码点），故起点仍在 seq1 偏移 3，终点跨到
    # seq2 偏移 2（源快照中终点为 seq2 偏移 5，切分点已不同）
    assert body["match"] == {
        "start_seq": 1, "start_offset": 3, "end_seq": 2, "end_offset": 2,
    }


def test_migrate_quote_offset_round_trip_covers_full_text(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 2, 1, "def")
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    # 整条跨字幕引用（起点 0、终点等于末条长度）
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 2, 3)
    body = resp.json()
    assert body["status"] == "found"
    assert body["quote"] == "abcdef"
    assert body["match"] == {
        "start_seq": 1, "start_offset": 0, "end_seq": 2, "end_offset": 3,
    }


# ---------- missing / ambiguous ----------


def test_migrate_quote_missing(client):
    submit(client, "c1", 1, 1, "hello")
    src = make_snapshot(client, "c1", "s1")

    correct(client, "c1", 1, 0, "world")
    tgt = make_snapshot(client, "c1", "t1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 5)
    body = resp.json()
    assert body["quote"] == "hello"
    assert body["status"] == "missing"
    assert body["match"] is None


def test_migrate_quote_ambiguous_does_not_pick_one(client):
    submit(client, "c1", 1, 1, "echo echo")
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 4)
    body = resp.json()
    assert body["quote"] == "echo"
    assert body["status"] == "ambiguous"
    assert body["match"] is None  # 绝不任选一个位置


def test_migrate_quote_ambiguous_across_segments(client):
    # 两次命中分别位于被缺口隔开的两段中，仍计为多次
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 3, 1, "abc")
    decide_gap(client, "c1", 2, 2)
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 3)
    body = resp.json()
    assert body["status"] == "ambiguous"
    assert body["match"] is None


def test_migrate_quote_overlapping_occurrences_are_multiple(client):
    submit(client, "c1", 1, 1, "aaa")
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    # "aa" 在 "aaa" 中有 0 和 1 两个重叠出现 -> ambiguous
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 2)
    body = resp.json()
    assert body["quote"] == "aa"
    assert body["status"] == "ambiguous"


# ---------- 缺口阻断 ----------


def test_migrate_quote_source_range_cannot_cross_gap(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 3, 1, "def")
    decide_gap(client, "c1", 2, 2)
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 3, 3)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_quote_range"


def test_migrate_quote_gap_in_target_blocks_match(client):
    # 定稿序号不可变：同频道里“连续 -> 缺口”的目标只能借助缺口裁决前
    # 先取的（更早的）快照构造。先裁决 seq2 缺口、定稿 seq1="abc" 与
    # seq3="def"，取目标快照（两段被缺口隔开）；随后只经更正改变文本，
    # 再取源快照（序号结构相同，但引用内容恰好横跨目标中的缺口边界）。
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 3, 1, "def")
    decide_gap(client, "c1", 2, 2)  # seq2 裁决为缺口，水位线推进到 3
    tgt = make_snapshot(client, "c1", "t1")

    correct(client, "c1", 1, 0, "ab cdef-tail")  # 源 seq1 内含 "cdef"
    src = make_snapshot(client, "c1", "s1")

    # 若把缺口两侧无分隔拼起来，目标文本是 "abc"+"def"="abcdef"，其中
    # 恰含 "cdef"；但缺口必须阻断拼接，故结果应为 missing
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 3, 1, 7)
    body = resp.json()
    assert body["quote"] == "cdef"
    assert body["status"] == "missing"

    # 段内的引用（源 seq3 前缀 "def"，目标 seg3 全文 "def"）正常命中
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 3, 0, 3, 3)
    body = resp.json()
    assert body["quote"] == "def"
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 3, "start_offset": 0, "end_seq": 3, "end_offset": 3,
    }


# ---------- 冻结生效文本 ----------


def test_migrate_quote_uses_frozen_effective_text(client):
    submit(client, "c1", 1, 1, "原始内容")
    correct(client, "c1", 1, 0, "生效内容")
    src = make_snapshot(client, "c1", "s1")

    # 快照后再次更正，不影响两个既有快照
    correct(client, "c1", 1, 1, "快照后内容")
    tgt = make_snapshot(client, "c1", "t1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 4)
    body = resp.json()
    # 源引用来自 src 冻结的生效文本“生效内容”
    assert body["quote"] == "生效内容"
    # tgt 冻结的生效文本是“快照后内容” -> missing
    assert body["status"] == "missing"

    # 目标同样冻结：再改实时文本后重查，结果不变
    correct(client, "c1", 1, 2, "生效内容")
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 4)
    assert resp.json()["status"] == "missing"


# ---------- Unicode 码点偏移 ----------


def test_migrate_quote_offsets_are_unicode_code_points(client):
    text = "你好世界"  # 4 个码点；UTF-8 下 12 字节
    submit(client, "c1", 1, 1, text)
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 2, 1, 4)
    body = resp.json()
    assert body["status"] == "found"
    assert body["quote"] == "世界"
    assert body["match"] == {
        "start_seq": 1, "start_offset": 2, "end_seq": 1, "end_offset": 4,
    }

    # 偏移按码点计：文本只有 4 码点，按字节给出的 12 越界 -> 422
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 12)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_quote_range"

    # 不做归一化：源为分解形式 e + U+0301（2 码点），目标经更正变为
    # 预组合形式 U+00E9（1 码点），逐码点精确匹配 -> missing
    decomposed = chr(0x65) + chr(0x301)  # e + U+0301，两个码点
    composed = chr(0xE9)  # U+00E9，单个码点
    submit(client, "c2", 1, 1, decomposed + "x")
    src2 = make_snapshot(client, "c2", "s1")
    correct(client, "c2", 1, 0, composed + "x")
    tgt2 = make_snapshot(client, "c2", "t1")
    resp = migrate(client, src2["snapshot_id"], tgt2["snapshot_id"], 1, 0, 1, 2)
    body = resp.json()
    assert body["quote"] == decomposed  # 分解形式，恰好两个码点
    assert len(body["quote"]) == 2
    assert body["status"] == "missing"


# ---------- 空字幕 / 空快照 ----------


def test_migrate_quote_can_span_empty_intermediate_subtitle(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 2, 1, "")
    submit(client, "c1", 3, 1, "def")
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    # seq2 为空但仍是相邻字幕：seq1[1:] + "" + seq3[:2] = "bcde"
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 1, 3, 2)
    body = resp.json()
    assert body["quote"] == "bcde"
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 1, "start_offset": 1, "end_seq": 3, "end_offset": 2,
    }

    # 仅覆盖空字幕本身（0..0）为空引用 -> 422
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 2, 0, 2, 0)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_quote_range"


def test_migrate_quote_from_empty_source_snapshot_range_rejected(client):
    src = make_snapshot(client, "ghost", "s1")
    tgt = src

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 1)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_quote_range"


def test_migrate_quote_to_empty_target_snapshot_is_missing(client):
    # 空快照只能在频道尚无定稿内容时创建；快照不可变，之后定稿不影响它
    tgt = make_snapshot(client, "c1", "empty")
    assert tgt["watermark"] == 0
    submit(client, "c1", 1, 1, "abc")
    src = make_snapshot(client, "c1", "s1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 3)
    body = resp.json()
    assert body["status"] == "missing"
    assert body["match"] is None


# ---------- 源区间校验 ----------


def test_migrate_quote_invalid_source_ranges(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 2, 1, "defg")
    src = make_snapshot(client, "c1", "s1")
    tgt = src

    # 起点偏移越界（> seq1 文本长度 3）：schema 无法知晓文本长度，
    # 由业务层返回 422 invalid_quote_range
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 4, 2, 1)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_quote_range"

    # 终点偏移越界（> seq2 文本长度 4）
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 1, 2, 5)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_quote_range"

    # 同一字幕零宽引用（start_offset == end_offset）
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 1, 1, 1)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_quote_range"

    # 跨字幕零宽引用（恰好在边界、两侧都不取字）
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 3, 2, 0)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_quote_range"

    # 序号越过源快照水位线
    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 3, 1)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_quote_range"


def test_migrate_quote_request_body_validation(client):
    submit(client, "c1", 1, 1, "abc")
    src = make_snapshot(client, "c1", "s1")["snapshot_id"]

    def raw(payload):
        return client.post(f"/snapshots/{src}/quote-migrations", json=payload)

    base = {
        "target_snapshot_id": src,
        "start_seq": 1, "start_offset": 0,
        "end_seq": 1, "end_offset": 1,
    }
    # 缺字段
    bad = dict(base)
    del bad["target_snapshot_id"]
    assert raw(bad).status_code == 422
    # 序号 < 1、偏移 < 0
    bad = dict(base, start_seq=0)
    assert raw(bad).status_code == 422
    bad = dict(base, start_offset=-1)
    assert raw(bad).status_code == 422
    # end_seq < start_seq
    bad = dict(base, end_seq=0)
    assert raw(bad).status_code == 422
    # 同字幕 end_offset < start_offset
    bad = dict(base, start_offset=2, end_offset=1)
    assert raw(bad).status_code == 422
    # 空白 target_snapshot_id
    bad = dict(base, target_snapshot_id="   ")
    assert raw(bad).status_code == 422


# ---------- 快照存在性 / 同频道 ----------


def test_migrate_quote_unknown_snapshots_return_404(client):
    submit(client, "c1", 1, 1, "abc")
    src = make_snapshot(client, "c1", "s1")["snapshot_id"]

    # 源快照不存在
    resp = migrate(client, "missing-source", src, 1, 0, 1, 1)
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"

    # 目标快照不存在
    resp = migrate(client, src, "missing-target", 1, 0, 1, 1)
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"


def test_migrate_quote_different_channels_rejected(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c2", 1, 1, "abc")
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c2", "t1")

    resp = migrate(client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 3)
    assert resp.status_code == 409
    assert resp.json()["error"] == "snapshot_channel_mismatch"


def test_migrate_quote_same_snapshot_is_allowed(client):
    submit(client, "c1", 1, 1, "only one")
    snap = make_snapshot(client, "c1", "s1")["snapshot_id"]

    resp = migrate(client, snap, snap, 1, 5, 1, 8)  # "one"
    body = resp.json()
    assert body["status"] == "found"
    assert body["quote"] == "one"
    assert body["match"]["start_seq"] == 1


# ---------- 只读 ----------


def test_migrate_quote_is_read_only(client):
    submit(client, "c1", 1, 1, "敏感引用")
    correct(client, "c1", 1, 0, "敏感引用！")
    src = make_snapshot(client, "c1", "s1")
    tgt = make_snapshot(client, "c1", "t1")

    resp1 = migrate(
        client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 4
    ).json()
    resp2 = migrate(
        client, src["snapshot_id"], tgt["snapshot_id"], 1, 0, 1, 4
    ).json()
    assert resp1 == resp2

    # 两个快照的冻结内容均不变
    src_eff = client.get(
        f"/snapshots/{src['snapshot_id']}/items", params={"view": "effective"}
    ).json()
    tgt_eff = client.get(
        f"/snapshots/{tgt['snapshot_id']}/items", params={"view": "effective"}
    ).json()
    assert src_eff["items"][0]["text"] == "敏感引用！"
    assert tgt_eff["items"][0]["text"] == "敏感引用！"

    # 当前定稿 / 更正链不受影响
    live = client.get("/channels/c1/finalized", params={"view": "effective"}).json()
    assert live["items"][0]["text"] == "敏感引用！"
    audit = client.get("/channels/c1/finalized/1/corrections").json()
    assert audit["current_version"] == 1
