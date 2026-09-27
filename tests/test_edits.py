"""剪辑字幕导出测试。

覆盖：
- 基于快照冻结生效文本的导出（快照后的新更正 / 新定稿不影响结果）；
- 每条字幕与每个 clip 取左闭右开交集，非空才生成 cue 并平移到成片
  累计时间；跨片段拆分、重复片段保留副本、重叠字幕不合并；
- cue 按 (start_ms, clip_index, seq) 升序返回；
- format=vtt 输出合法 WebVTT（WEBVTT 头、HH:MM:SS.mmm 时间戳、
  正文转义 & < >）；
- timings 须恰好覆盖全部字幕 seq：缺序号 / 重复 / 多余（含缺口序号）
  均 422；时间非负、起点小于终点等校验 422；快照不存在 404；
- 只读：导出不修改快照内容，重复导出结果一致。
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


def export(client, snapshot_id, timings, clips, **params):
    payload = {"timings": timings, "clips": clips}
    return client.post(f"/snapshots/{snapshot_id}/edits", json=payload, params=params)


def timing(seq, start_ms, end_ms):
    return {"seq": seq, "start_ms": start_ms, "end_ms": end_ms}


def clip(start_ms, end_ms):
    return {"start_ms": start_ms, "end_ms": end_ms}


def make_two_subtitle_snapshot(client):
    submit(client, "c1", 1, 1, "第一条")
    submit(client, "c1", 2, 1, "第二条")
    return make_snapshot(client, "c1", "k1")["snapshot_id"]


# ---------- 基本交集与平移语义 ----------


def test_edit_basic_intersection_and_cumulative_shift(client):
    sid = make_two_subtitle_snapshot(client)
    body = export(
        client,
        sid,
        [timing(1, 0, 2000), timing(2, 2000, 4000)],
        [clip(0, 2000), clip(2000, 4000)],
    ).json()
    assert body["snapshot_id"] == sid
    assert body["channel"] == "c1"
    assert body["format"] == "json"
    assert body["duration_ms"] == 4000
    assert body["cues"] == [
        {"seq": 1, "clip_index": 0, "text": "第一条", "start_ms": 0, "end_ms": 2000},
        {"seq": 2, "clip_index": 1, "text": "第二条", "start_ms": 2000, "end_ms": 4000},
    ]


def test_edit_subtitle_split_across_clips(client):
    sid = make_two_subtitle_snapshot(client)
    # 字幕 1 [0,3000) 横跨两个 clip：各生成一条 cue，平移到成片累计时间；
    # 字幕 2 与任何 clip 都无交集，不生成 cue
    body = export(
        client,
        sid,
        [timing(1, 0, 3000), timing(2, 5000, 6000)],
        [clip(0, 1000), clip(1000, 3000)],
    ).json()
    assert body["cues"] == [
        {"seq": 1, "clip_index": 0, "text": "第一条", "start_ms": 0, "end_ms": 1000},
        {"seq": 1, "clip_index": 1, "text": "第一条", "start_ms": 1000, "end_ms": 3000},
    ]
    assert body["duration_ms"] == 3000


def test_edit_clips_reordered(client):
    sid = make_two_subtitle_snapshot(client)
    # clips 重排：先源时间轴后段、再前段，按输入顺序拼接
    body = export(
        client,
        sid,
        [timing(1, 0, 1000), timing(2, 1000, 2000)],
        [clip(1000, 2000), clip(0, 1000)],
    ).json()
    assert body["cues"] == [
        {"seq": 2, "clip_index": 0, "text": "第二条", "start_ms": 0, "end_ms": 1000},
        {"seq": 1, "clip_index": 1, "text": "第一条", "start_ms": 1000, "end_ms": 2000},
    ]


def test_edit_duplicate_clip_keeps_copies(client):
    sid = make_two_subtitle_snapshot(client)
    # 同一 clip 出现两次：在不同成片偏移处各保留一份 cue 副本
    body = export(
        client,
        sid,
        [timing(1, 0, 1000), timing(2, 5000, 6000)],
        [clip(0, 1000), clip(0, 1000)],
    ).json()
    assert body["cues"] == [
        {"seq": 1, "clip_index": 0, "text": "第一条", "start_ms": 0, "end_ms": 1000},
        {"seq": 1, "clip_index": 1, "text": "第一条", "start_ms": 1000, "end_ms": 2000},
    ]


def test_edit_overlapping_clips(client):
    sid = make_two_subtitle_snapshot(client)
    # clip1 与字幕 1 的交集 [1000,2000) 平移到成片 [2000,3000)
    body = export(
        client,
        sid,
        [timing(1, 0, 2000), timing(2, 9000, 10000)],
        [clip(0, 2000), clip(1000, 3000)],
    ).json()
    assert body["cues"] == [
        {"seq": 1, "clip_index": 0, "text": "第一条", "start_ms": 0, "end_ms": 2000},
        {"seq": 1, "clip_index": 1, "text": "第一条", "start_ms": 2000, "end_ms": 3000},
    ]
    assert body["duration_ms"] == 4000


def test_edit_overlapping_subtitles_not_merged(client):
    sid = make_two_subtitle_snapshot(client)
    # 两条字幕时间重叠：各自生成 cue，不合并
    body = export(
        client,
        sid,
        [timing(1, 0, 2000), timing(2, 1000, 3000)],
        [clip(0, 3000)],
    ).json()
    assert body["cues"] == [
        {"seq": 1, "clip_index": 0, "text": "第一条", "start_ms": 0, "end_ms": 2000},
        {"seq": 2, "clip_index": 0, "text": "第二条", "start_ms": 1000, "end_ms": 3000},
    ]


def test_edit_empty_intersection_and_boundary(client):
    sid = make_two_subtitle_snapshot(client)
    # 左闭右开：字幕终点 == clip 起点时交集为空，不生成 cue
    body = export(
        client,
        sid,
        [timing(1, 0, 1000), timing(2, 3000, 4000)],
        [clip(1000, 3000)],
    ).json()
    assert body["cues"] == []
    assert body["duration_ms"] == 2000


def test_edit_cues_sorted_by_start_clip_index_seq(client):
    submit(client, "c1", 1, 1, "A")
    submit(client, "c1", 2, 1, "B")
    submit(client, "c1", 3, 1, "C")
    sid = make_snapshot(client, "c1", "k1")["snapshot_id"]
    # timings 输入乱序、clips 重复：输出按 (start_ms, clip_index, seq) 升序
    body = export(
        client,
        sid,
        [timing(3, 0, 1000), timing(1, 0, 1000), timing(2, 0, 1000)],
        [clip(0, 1000), clip(0, 1000)],
    ).json()
    assert [(c["start_ms"], c["clip_index"], c["seq"]) for c in body["cues"]] == [
        (0, 0, 1),
        (0, 0, 2),
        (0, 0, 3),
        (1000, 1, 1),
        (1000, 1, 2),
        (1000, 1, 3),
    ]


# ---------- 冻结文本与只读语义 ----------


def test_edit_uses_frozen_effective_text(client):
    submit(client, "c1", 1, 1, "原始文本")
    correct(client, "c1", 1, 0, "快照时更正")
    sid = make_snapshot(client, "c1", "k1")["snapshot_id"]

    # 快照后再更正、再定稿，均不影响既有快照的导出
    correct(client, "c1", 1, 1, "快照后再改")
    submit(client, "c1", 2, 1, "第二条")

    body = export(client, sid, [timing(1, 0, 1000)], [clip(0, 1000)]).json()
    assert body["cues"][0]["text"] == "快照时更正"


def test_edit_does_not_modify_snapshot(client):
    submit(client, "c1", 1, 1, "原始")
    correct(client, "c1", 1, 0, "更正")
    sid = make_snapshot(client, "c1", "k1")["snapshot_id"]

    body = export(client, sid, [timing(1, 0, 1000)], [clip(0, 1000)]).json()
    assert body["cues"][0]["text"] == "更正"

    # 快照两个视图与快照内审计均保持原样
    original = client.get(f"/snapshots/{sid}/items").json()
    assert original["items"][0]["text"] == "原始"
    effective = client.get(
        f"/snapshots/{sid}/items", params={"view": "effective"}
    ).json()
    assert effective["items"][0]["text"] == "更正"
    assert effective["items"][0]["correction_version"] == 1

    # 重复导出结果稳定（幂等的纯计算）
    again = export(client, sid, [timing(1, 0, 1000)], [clip(0, 1000)]).json()
    assert again == body


# ---------- 空快照 / 空 clips ----------


def test_edit_empty_snapshot(client):
    sid = make_snapshot(client, "ghost", "k1")["snapshot_id"]
    # 空快照（无字幕）：空 timings 恰好覆盖，导出为空
    body = export(client, sid, [], [clip(0, 1000)]).json()
    assert body["cues"] == []
    assert body["duration_ms"] == 1000

    # 空快照传非空 timings：包含多余序号 -> 422
    resp = export(client, sid, [timing(1, 0, 1000)], [clip(0, 1000)])
    assert resp.status_code == 422
    assert resp.json()["error"] == "timings_mismatch"


def test_edit_empty_clips(client):
    sid = make_two_subtitle_snapshot(client)
    body = export(
        client, sid, [timing(1, 0, 1000), timing(2, 1000, 2000)], []
    ).json()
    assert body["cues"] == []
    assert body["duration_ms"] == 0


# ---------- timings 覆盖校验与参数校验 ----------


def test_edit_timings_must_cover_all_subtitle_seqs(client):
    submit(client, "c1", 1, 1, "甲")
    submit(client, "c1", 3, 1, "丙")
    decide_gap(client, "c1", 2, 2)
    sid = make_snapshot(client, "c1", "k1")["snapshot_id"]
    clips = [clip(0, 1000)]

    # 缺少字幕 seq 3
    resp = export(client, sid, [timing(1, 0, 1000)], clips)
    assert resp.status_code == 422
    assert resp.json()["error"] == "timings_mismatch"

    # 重复 seq
    resp = export(
        client,
        sid,
        [timing(1, 0, 1000), timing(1, 0, 1000), timing(3, 0, 1000)],
        clips,
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "timings_mismatch"

    # 多余 seq：缺口序号 2 与超界序号 9 都不是快照字幕
    resp = export(
        client, sid, [timing(1, 0, 1000), timing(2, 0, 1000), timing(3, 0, 1000)], clips
    )
    assert resp.status_code == 422
    resp = export(
        client, sid, [timing(1, 0, 1000), timing(3, 0, 1000), timing(9, 0, 1000)], clips
    )
    assert resp.status_code == 422

    # 恰好覆盖（含缺口的快照只要求字幕 seq）：缺口序号无需 timing
    resp = export(client, sid, [timing(1, 0, 1000), timing(3, 0, 1000)], clips)
    assert resp.status_code == 200
    assert [c["seq"] for c in resp.json()["cues"]] == [1, 3]


def test_edit_time_validation(client):
    sid = make_two_subtitle_snapshot(client)
    valid = [timing(1, 0, 1000), timing(2, 1000, 2000)]

    # 起点不小于终点（timing 与 clip 同样校验）
    assert export(
        client, sid, [{"seq": 1, "start_ms": 1000, "end_ms": 1000}, timing(2, 1000, 2000)],
        [clip(0, 1000)],
    ).status_code == 422
    assert export(
        client, sid, [{"seq": 1, "start_ms": 2000, "end_ms": 1000}, timing(2, 1000, 2000)],
        [clip(0, 1000)],
    ).status_code == 422
    assert export(
        client, sid, valid, [{"start_ms": 500, "end_ms": 500}]
    ).status_code == 422
    # 负时间
    assert export(
        client, sid, [{"seq": 1, "start_ms": -1, "end_ms": 1000}, timing(2, 1000, 2000)],
        [clip(0, 1000)],
    ).status_code == 422
    assert export(
        client, sid, valid, [{"start_ms": 0, "end_ms": -1}]
    ).status_code == 422
    # 非整数时间
    assert export(
        client, sid, [{"seq": 1, "start_ms": 0.5, "end_ms": 1000}, timing(2, 1000, 2000)],
        [clip(0, 1000)],
    ).status_code == 422
    # seq 必须 >= 1
    assert export(
        client, sid, [{"seq": 0, "start_ms": 0, "end_ms": 1000}, timing(2, 1000, 2000)],
        [clip(0, 1000)],
    ).status_code == 422
    # 缺字段 / 空请求体
    assert export(
        client, sid, [{"seq": 1, "start_ms": 0}], [clip(0, 1000)]
    ).status_code == 422
    assert client.post(f"/snapshots/{sid}/edits", json={}).status_code == 422


def test_edit_unknown_snapshot_returns_404(client):
    resp = export(client, "does-not-exist", [timing(1, 0, 1000)], [clip(0, 1000)])
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"


# ---------- WebVTT 输出 ----------


def test_edit_vtt_output(client):
    submit(client, "c1", 1, 1, "第一条 & <字幕>")
    submit(client, "c1", 2, 1, "第二条")
    sid = make_snapshot(client, "c1", "k1")["snapshot_id"]

    resp = export(
        client,
        sid,
        [timing(1, 0, 1500), timing(2, 1500, 3723456)],
        [clip(0, 4000000)],
        format="vtt",
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/vtt")
    assert resp.text == (
        "WEBVTT\n"
        "\n"
        "1\n"
        "00:00:00.000 --> 00:00:01.500\n"
        "第一条 &amp; &lt;字幕&gt;\n"
        "\n"
        "2\n"
        "00:00:01.500 --> 01:02:03.456\n"
        "第二条\n"
        "\n"
    )


def test_edit_vtt_empty_cues(client):
    sid = make_two_subtitle_snapshot(client)
    resp = export(
        client,
        sid,
        [timing(1, 0, 1000), timing(2, 1000, 2000)],
        [clip(5000, 6000)],
        format="vtt",
    )
    assert resp.status_code == 200
    assert resp.text == "WEBVTT\n\n"


def test_edit_invalid_format_returns_422(client):
    sid = make_two_subtitle_snapshot(client)
    resp = export(
        client,
        sid,
        [timing(1, 0, 1000), timing(2, 1000, 2000)],
        [clip(0, 2000)],
        format="srt",
    )
    assert resp.status_code == 422
