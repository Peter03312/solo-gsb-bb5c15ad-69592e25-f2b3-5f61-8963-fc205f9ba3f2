"""稿件校稿对齐测试。

覆盖：
- 逐行 1:1 对齐、连续 1~3 条字幕合并匹配、跳过字幕 / 稿件行；
- 匹配不得跨越缺口、字幕不得复用，行序与字幕序都保持单调；
- Unicode 码点编辑距离与跳过码点代价（非字节、不归一化）；
- 并列裁决：总代价相同依次取跳过项更少、合并字幕更少、匹配序号字典序更小；
- 只读快照冻结的生效文本（快照后更正不影响结果），重复调用结果稳定；
- 422（行数越界 / 空白行 / max_subtitles 越界 / 缺字段）与 404（快照不存在）。
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
        json={"expected_version": expected_version, "text": text, "reason": reason},
    )
    assert resp.status_code == 200
    return resp.json()


def make_snapshot(client, channel, key):
    resp = client.post(
        f"/channels/{channel}/snapshots", json={"idempotency_key": key}
    )
    assert resp.status_code == 200
    return resp.json()


def align(client, snapshot_id, lines):
    return client.post(
        f"/snapshots/{snapshot_id}/script-alignments",
        json={"lines": lines},
    )


def L(text, max_subtitles=3):
    return {"text": text, "max_subtitles": max_subtitles}


# ---------- 基本对齐 ----------


def test_exact_one_to_one_alignment(client):
    submit(client, "c1", 1, 1, "第一条")
    submit(client, "c1", 2, 1, "第二条")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], [L("第一条"), L("第二条")])
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 0
    assert body["unmatched_script_lines"] == []
    assert body["unmatched_subtitle_seqs"] == []
    assert body["alignments"] == [
        {"index": 0, "matched": True, "start_seq": 1, "end_seq": 1,
         "subtitles": 1, "distance": 0},
        {"index": 1, "matched": True, "start_seq": 2, "end_seq": 2,
         "subtitles": 1, "distance": 0},
    ]


def test_alignment_with_edit_distance_and_skipped_subtitle(client):
    submit(client, "c1", 1, 1, "你好世界")
    submit(client, "c1", 2, 1, "无关字幕")
    snap = make_snapshot(client, "c1", "s1")

    # “你好世畍”与 seq1 的码点编辑距离为 1；max_subtitles=1 禁止合并，
    # seq2 只能被跳过，代价为其码点数 4
    resp = align(client, snap["snapshot_id"], [L("你好世畍", 1)])
    body = resp.json()
    assert body["total_cost"] == 5
    assert body["alignments"] == [
        {"index": 0, "matched": True, "start_seq": 1, "end_seq": 1,
         "subtitles": 1, "distance": 1},
    ]
    assert body["unmatched_subtitle_seqs"] == [2]
    assert body["unmatched_script_lines"] == []


def test_skip_line_cost_is_codepoint_length_on_empty_snapshot(client):
    snap = make_snapshot(client, "ghost", "s1")
    assert snap["watermark"] == 0

    resp = align(client, snap["snapshot_id"], [L("abc"), L("你好")])
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 5  # 3 + 2 个码点
    assert body["unmatched_script_lines"] == [0, 1]
    assert body["unmatched_subtitle_seqs"] == []
    assert body["alignments"] == [
        {"index": 0, "matched": False, "start_seq": None, "end_seq": None,
         "subtitles": None, "distance": None},
        {"index": 1, "matched": False, "start_seq": None, "end_seq": None,
         "subtitles": None, "distance": None},
    ]


def test_extra_script_lines_are_skipped(client):
    submit(client, "c1", 1, 1, "only")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(
        client, snap["snapshot_id"], [L("zzzz"), L("only"), L("extra")]
    )
    body = resp.json()
    # 行 0 跳过（4）+ 行 1 命中 seq1（0）+ 行 2 跳过（5）= 9
    assert body["total_cost"] == 9
    assert body["unmatched_script_lines"] == [0, 2]
    assert body["unmatched_subtitle_seqs"] == []
    assert body["alignments"][1] == {
        "index": 1, "matched": True, "start_seq": 1, "end_seq": 1,
        "subtitles": 1, "distance": 0,
    }


# ---------- 合并字幕 ----------


def test_merge_consecutive_subtitles_up_to_three(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 2, 1, "def")
    submit(client, "c1", 3, 1, "ghi")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], [L("abcdefghi", 3), L("尾行")])
    body = resp.json()
    # 行 0 合并 seq1..3（无分隔拼接）精确命中；行 1 无字幕可匹配，跳过代价 2
    assert body["total_cost"] == 2
    assert body["alignments"][0] == {
        "index": 0, "matched": True, "start_seq": 1, "end_seq": 3,
        "subtitles": 3, "distance": 0,
    }
    assert body["alignments"][1]["matched"] is False
    assert body["unmatched_script_lines"] == [1]


def test_merge_respects_per_line_max_subtitles(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 2, 1, "def")
    snap = make_snapshot(client, "c1", "s1")

    # max_subtitles=2：允许合并 seq1+2 精确命中
    resp = align(client, snap["snapshot_id"], [L("abcdef", 2)])
    body = resp.json()
    assert body["total_cost"] == 0
    assert body["alignments"][0]["start_seq"] == 1
    assert body["alignments"][0]["end_seq"] == 2
    assert body["alignments"][0]["subtitles"] == 2

    # max_subtitles=1：禁止合并；匹配 seq1 距离 3，seq2 跳过代价 3
    resp = align(client, snap["snapshot_id"], [L("abcdef", 1)])
    body = resp.json()
    assert body["total_cost"] == 6
    assert body["alignments"][0] == {
        "index": 0, "matched": True, "start_seq": 1, "end_seq": 1,
        "subtitles": 1, "distance": 3,
    }
    assert body["unmatched_subtitle_seqs"] == [2]


def test_merge_two_of_three_then_match_third(client):
    submit(client, "c1", 1, 1, "hello ")
    submit(client, "c1", 2, 1, "world")
    submit(client, "c1", 3, 1, "！")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(
        client,
        snap["snapshot_id"],
        [L("hello world", 2), L("！", 1)],
    )
    body = resp.json()
    assert body["total_cost"] == 0
    assert body["alignments"] == [
        {"index": 0, "matched": True, "start_seq": 1, "end_seq": 2,
         "subtitles": 2, "distance": 0},
        {"index": 1, "matched": True, "start_seq": 3, "end_seq": 3,
         "subtitles": 1, "distance": 0},
    ]
    assert body["unmatched_subtitle_seqs"] == []
    assert body["unmatched_script_lines"] == []


def test_match_does_not_cross_gap(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 3, 1, "def")
    decide_gap(client, "c1", 2, 2)
    snap = make_snapshot(client, "c1", "s1")

    # 不得把缺口两侧的 seq1 与 seq3 无分隔拼成 "abcdef" 精确命中
    resp = align(client, snap["snapshot_id"], [L("abcdef", 3)])
    body = resp.json()
    # 只能匹缺口一侧：距离 3 + 跳过另一侧字幕 3 码点 = 6；字典序取序号更小的块
    assert body["total_cost"] == 6
    assert body["alignments"][0] == {
        "index": 0, "matched": True, "start_seq": 1, "end_seq": 1,
        "subtitles": 1, "distance": 3,
    }
    assert body["unmatched_subtitle_seqs"] == [3]


def test_subtitles_are_never_reused_and_order_is_monotonic(client):
    # 两行相同稿件、两条不同字幕：seq1 已被行 0 命中后，行 1 绝不能
    # 复用 seq1（只能匹配 seq2 或被跳过）；命中块的序号严格递增
    submit(client, "c1", 1, 1, "x")
    submit(client, "c1", 2, 1, "y")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], [L("x", 1), L("x", 1)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 1  # 行0 命中 seq1（0）+ 行1 与 seq2 距离 1 或跳过代价 1

    first = body["alignments"][0]
    assert first["matched"] is True
    assert first["start_seq"] == 1

    second = body["alignments"][1]
    # 行 1 无论命中还是跳过，都不可能再引用 seq1
    if second["matched"]:
        assert second["start_seq"] == 2
        assert body["unmatched_script_lines"] == []
        assert body["unmatched_subtitle_seqs"] == []
    else:
        assert body["unmatched_script_lines"] == [1]
        assert body["unmatched_subtitle_seqs"] == [2]

    # 通用不变式：命中块序号严格递增、互不重叠
    matched = [a for a in body["alignments"] if a["matched"]]
    for prev_item, next_item in zip(matched, matched[1:]):
        assert prev_item["end_seq"] < next_item["start_seq"]
        assert prev_item["start_seq"] <= prev_item["end_seq"]


# ---------- 并列裁决 ----------


def test_tie_prefers_fewer_skips(client):
    # seq1="abc" 与 seq2="abc"：单行 max_subtitles=2 时有两个同价方案——
    # A: 合并 seq1+2（"abcabc" 与 "abc" 距离 3），跳过项 0；
    # B: 只命中 seq1（距离 0）并跳过 seq2（3 码点），总代价同样 3、跳过项 1。
    # 跳过项更少的 A 胜出。
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 2, 1, "abc")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], [L("abc", 2)])
    body = resp.json()
    assert body["total_cost"] == 3
    assert body["unmatched_subtitle_seqs"] == []
    assert body["alignments"][0] == {
        "index": 0, "matched": True, "start_seq": 1, "end_seq": 2,
        "subtitles": 2, "distance": 3,
    }

    # max_subtitles=1 时方案 A 不可行，只能命中 seq1 + 跳过 seq2
    resp = align(client, snap["snapshot_id"], [L("abc", 1)])
    body = resp.json()
    assert body["total_cost"] == 3
    assert body["unmatched_subtitle_seqs"] == [2]
    assert body["alignments"][0]["start_seq"] == 1


def test_tie_on_all_costs_prefers_lexicographically_smaller_match_seqs(client):
    # 三条空字幕 + 两行非空稿件：任何匹配的距离都是行码点数，所有
    # “无跳过 + 一次合并”方案总代价 / 跳过项 / 合并数完全相同，
    # 最终由匹配序号字典序裁决：[1, [2,3]] 优于 [[1,2], 3]
    submit(client, "c1", 1, 1, "")
    submit(client, "c1", 2, 1, "")
    submit(client, "c1", 3, 1, "")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], [L("a", 3), L("b", 3)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 2
    assert body["unmatched_subtitle_seqs"] == []
    assert body["alignments"] == [
        {"index": 0, "matched": True, "start_seq": 1, "end_seq": 1,
         "subtitles": 1, "distance": 1},
        {"index": 1, "matched": True, "start_seq": 2, "end_seq": 3,
         "subtitles": 2, "distance": 1},
    ]


def test_tie_between_match_and_skip_subtitle_prefers_match(client):
    # 两行稿件、两条字幕：line0 精确命中 seq1；line1 与 seq2 互不相关
    # （"y" vs "x"，距离 1）。“行1 命中 seq2（距离 1）”与“行1 跳过 +
    # seq2 跳过（共 2 码点）”相比命中更省；即使与“仅跳过 seq2”
    # 同价，命中方案跳过项也更少（0 vs 1），稳定选择命中。
    submit(client, "c1", 1, 1, "same")
    submit(client, "c1", 2, 1, "x")
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], [L("same", 1), L("y", 1)])
    body = resp.json()
    assert body["total_cost"] == 1
    assert body["alignments"] == [
        {"index": 0, "matched": True, "start_seq": 1, "end_seq": 1,
         "subtitles": 1, "distance": 0},
        {"index": 1, "matched": True, "start_seq": 2, "end_seq": 2,
         "subtitles": 1, "distance": 1},
    ]
    assert body["unmatched_script_lines"] == []
    assert body["unmatched_subtitle_seqs"] == []


# ---------- Unicode 码点 ----------


def test_distance_uses_unicode_code_points_not_bytes(client):
    text = "你好"  # 2 个码点；UTF-8 下 6 字节
    submit(client, "c1", 1, 1, text)
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], [L("你号", 1)])
    body = resp.json()
    assert body["alignments"][0]["distance"] == 1
    assert body["total_cost"] == 1


def test_no_unicode_normalization(client):
    # 分解形式 e + U+0301（2 码点）vs 预组合 U+00E9（1 码点）：逐码点不同
    decomposed = chr(0x65) + chr(0x301) + "x"
    composed = chr(0xE9) + "x"
    submit(client, "c1", 1, 1, decomposed)
    snap = make_snapshot(client, "c1", "s1")

    resp = align(client, snap["snapshot_id"], [L(composed, 1)])
    body = resp.json()
    # "éx"(2) 与 "é 分解 + x"(3)：距离 2（替换+删除）而非 1
    assert body["alignments"][0]["distance"] == 2


def test_blank_script_line_is_stripped_before_distance(client):
    submit(client, "c1", 1, 1, "abc")
    snap = make_snapshot(client, "c1", "s1")

    # 纯空白非法（422）；首尾空白在去空白后参与匹配
    resp = align(client, snap["snapshot_id"], [L("  abc  ", 1)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["alignments"][0]["distance"] == 0


# ---------- 冻结生效文本 / 只读 ----------


def test_alignment_uses_frozen_effective_text(client):
    submit(client, "c1", 1, 1, "原始字幕")
    correct(client, "c1", 1, 0, "生效字幕")
    snap = make_snapshot(client, "c1", "s1")

    # 快照之后再更正，不影响既有快照
    correct(client, "c1", 1, 1, "快照后字幕")

    resp = align(client, snap["snapshot_id"], [L("生效字幕", 1)])
    body = resp.json()
    assert body["total_cost"] == 0
    assert body["alignments"][0]["distance"] == 0

    resp = align(client, snap["snapshot_id"], [L("快照后字幕", 1)])
    body = resp.json()
    # "快照后字幕" vs 冻结的 "生效字幕"：替换 3 码点（快照后 -> 生效）
    assert body["alignments"][0]["distance"] == 3


def test_alignment_is_read_only(client):
    submit(client, "c1", 1, 1, "hello")
    snap = make_snapshot(client, "c1", "s1")

    r1 = align(client, snap["snapshot_id"], [L("hello", 1)]).json()
    r2 = align(client, snap["snapshot_id"], [L("hello", 1)]).json()
    assert r1 == r2

    items = client.get(
        f"/snapshots/{snap['snapshot_id']}/items", params={"view": "effective"}
    ).json()
    assert items["items"][0]["text"] == "hello"


# ---------- 校验 / 错误 ----------


def test_request_validation_failures(client):
    snap = make_snapshot(client, "c1", "s1")["snapshot_id"]
    url = f"/snapshots/{snap}/script-alignments"

    def raw(payload):
        return client.post(url, json=payload)

    # 缺 lines
    assert raw({}).status_code == 422
    # 0 行
    assert raw({"lines": []}).status_code == 422
    # 101 行
    too_many = [L("x", 1) for _ in range(101)]
    assert raw({"lines": too_many}).status_code == 422
    # 空文本 / 纯空白
    assert raw({"lines": [{"text": "", "max_subtitles": 1}]}).status_code == 422
    assert raw({"lines": [{"text": "   ", "max_subtitles": 1}]}).status_code == 422
    # max_subtitles 越界 / 缺失
    assert raw({"lines": [{"text": "a", "max_subtitles": 0}]}).status_code == 422
    assert raw({"lines": [{"text": "a", "max_subtitles": 4}]}).status_code == 422
    assert raw({"lines": [{"text": "a"}]}).status_code == 422


def test_unknown_snapshot_returns_404(client):
    resp = align(client, "no-such-snapshot", [L("a", 1)])
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"


def test_large_batch_of_one_hundred_lines_accepted(client):
    for seq in range(1, 101):
        submit(client, "c1", seq, 1, f"字幕{seq}")
    snap = make_snapshot(client, "c1", "s1")

    lines = [L(f"字幕{seq}", 3) for seq in range(1, 101)]
    resp = align(client, snap["snapshot_id"], lines)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 0
    assert len(body["alignments"]) == 100
    assert body["alignments"][0] == {
        "index": 0, "matched": True, "start_seq": 1, "end_seq": 1,
        "subtitles": 1, "distance": 0,
    }
    assert body["alignments"][-1]["start_seq"] == 100
