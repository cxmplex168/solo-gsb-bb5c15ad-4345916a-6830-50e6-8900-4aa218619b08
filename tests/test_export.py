"""export 模块的无第三方依赖验证脚本（python3 tests/test_export.py）。

直接用 app.db 的建表语句在临时 SQLite 上构造两轮完整演练，
校验：水位边界、脱敏、空演练、答复版本轨迹、时钟动作、时间线
虚拟时间与同刻事件按 ID 排序、ZIP 与离线回放页可生成。
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db as dbmod  # noqa: E402
from app import export as rv  # noqa: E402


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(dbmod.SCHEMA)
    return conn


def add_event(conn, room_id, t, type_, payload):
    cur = conn.execute(
        "INSERT INTO events (room_id, type, payload, created_at) VALUES (?,?,?,?)",
        (room_id, type_, json.dumps(payload, ensure_ascii=False), t),
    )
    conn.commit()
    return cur.lastrowid


def make_room(conn, name, status="paused", elapsed_ms=0, last_resume=None):
    cur = conn.execute(
        "INSERT INTO rooms (code,name,status,elapsed_ms,last_resume_at,created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("ABC234", name, status, elapsed_ms, last_resume, time.time()),
    )
    conn.commit()
    return cur.lastrowid


def test_empty_drill():
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        rid = make_room(conn, "空演练")
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone()
        data = rv.build_review_data(conn, room, 0)
        assert data["high_water_event_id"] == 0
        assert data["orchestration"]["groups"] == []
        assert data["orchestration"]["cards"] == []
        assert data["events"] == []
        assert data["clock_actions"] == []
        assert data["rounds"] == [{"round": 0, "ended": False, "end_ms": 0}]
        blob = rv.build_review_zip(data)
        with zipfile.ZipFile(__import__("io").BytesIO(blob)) as zf:
            names = set(zf.namelist())
            assert {"replay.html", "data.json", "README.txt"} <= names
            html = zf.read("replay.html").decode("utf-8")
            assert "__REPLAY_DATA_JSON__" not in html
            assert "空演练" in html
            again = json.loads(zf.read("data.json").decode("utf-8"))
            assert again["high_water_event_id"] == 0
        conn.close()
    print("ok: empty drill export opens and contains replay page")


def test_full_drill():
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        t0 = 1000.0
        rid = make_room(conn, "核心库故障")

        # 编排：两个组（带 join_code）、两张全员卡 + 一张门槛卡
        g1 = conn.execute(
            "INSERT INTO groups (room_id,name,join_code,created_at) VALUES (?,?,?,?)",
            (rid, "红队", "AAA111", t0)).lastrowid
        g2 = conn.execute(
            "INSERT INTO groups (room_id,name,join_code,created_at) VALUES (?,?,?,?)",
            (rid, "蓝队", "BBB222", t0)).lastrowid
        c1 = conn.execute(
            "INSERT INTO cards (room_id,seq,trigger_seconds,title,content,group_id)"
            " VALUES (?,?,?,?,?,?)", (rid, 1, 10, "告警", "CPU 高", None)).lastrowid
        c2 = conn.execute(
            "INSERT INTO cards (room_id,seq,trigger_seconds,title,content,group_id)"
            " VALUES (?,?,?,?,?,?)", (rid, 2, 20, "定向给红队", "排查", g1)).lastrowid
        c3 = conn.execute(
            "INSERT INTO cards (room_id,seq,trigger_seconds,title,content,group_id,"
            "depends_on_card_id,gate_timeout_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (rid, 3, 30, "门槛卡", "等前置", None, c1, 60)).lastrowid
        conn.commit()

        e_group1 = add_event(conn, rid, t0 + 1, "group_added",
                             {"id": g1, "name": "红队", "join_code": "AAA111"})
        e_group2 = add_event(conn, rid, t0 + 2, "group_added",
                             {"id": g2, "name": "蓝队", "join_code": "BBB222"})
        e_card1 = add_event(conn, rid, t0 + 3, "card_added",
                            {"id": c1, "seq": 1, "trigger_seconds": 10, "title": "告警",
                             "content": "CPU 高", "group_id": None})
        e_card2 = add_event(conn, rid, t0 + 4, "card_added",
                            {"id": c2, "seq": 2, "trigger_seconds": 20, "title": "定向给红队",
                             "content": "排查", "group_id": g1})
        e_card3 = add_event(conn, rid, t0 + 5, "card_added",
                            {"id": c3, "seq": 3, "trigger_seconds": 30, "title": "门槛卡",
                             "content": "等前置", "group_id": None})

        # 观察分享（含 token）——必须被剔除
        e_share = add_event(conn, rid, t0 + 6, "share_created",
                            {"token": "SECRET-TOKEN-XYZ", "group_ids": [g1]})

        # 开始（t0+10 墙钟 → 虚拟 0 起步）
        wall_start = t0 + 10
        e_start = add_event(conn, rid, wall_start, "clock",
                            {"action": "started", "status": "running", "elapsed_ms": 0})
        conn.execute("UPDATE rooms SET status='running', elapsed_ms=0, last_resume_at=? WHERE id=?",
                     (wall_start, rid))
        conn.commit()

        # 10s 揭示 c1
        conn.execute("UPDATE cards SET revealed=1, revealed_at_ms=? WHERE id=?", (10000, c1))
        conn.commit()
        e_rev1 = add_event(conn, rid, wall_start + 10.0, "card_revealed",
                           {"id": c1, "seq": 1, "trigger_seconds": 10, "title": "告警",
                            "group_id": None, "revealed": True, "revealed_at_ms": 10000,
                            "timed_out": False, "answers_locked": False})
        # 同刻（同一墙钟秒）红队回执、蓝队回执 —— 同刻按事件 ID 排序
        conn.execute("INSERT INTO receipts (room_id,card_id,group_id,created_at) VALUES (?,?,?,?)",
                     (rid, c1, g1, wall_start + 10.0))
        conn.execute("INSERT INTO receipts (room_id,card_id,group_id,created_at) VALUES (?,?,?,?)",
                     (rid, c1, g2, wall_start + 10.0))
        conn.commit()
        e_ack1 = add_event(conn, rid, wall_start + 10.0, "receipt_updated",
                           {"card_id": c1, "group_id": g1, "acked": True})
        e_ack2 = add_event(conn, rid, wall_start + 10.0, "receipt_updated",
                           {"card_id": c1, "group_id": g2, "acked": True})

        # 红队答复两个版本（v1 at 11s, v2 at 12s）
        conn.execute(
            "INSERT INTO answers (room_id,card_id,group_id,body,revision,updated_at)"
            " VALUES (?,?,?,?,1,?)", (rid, c1, g1, "初判：重启", wall_start + 11))
        conn.commit()
        e_a1 = add_event(conn, rid, wall_start + 11.0, "answer_updated",
                         {"card_id": c1, "group_id": g1, "body": "初判：重启", "revision": 1,
                          "updated_at": wall_start + 11})
        conn.execute("UPDATE answers SET body=?, revision=2, updated_at=? WHERE card_id=? AND group_id=?",
                     ("再判：扩容", wall_start + 12, c1, g1))
        conn.commit()
        e_a2 = add_event(conn, rid, wall_start + 12.0, "answer_updated",
                         {"card_id": c1, "group_id": g1, "body": "再判：扩容", "revision": 2,
                          "updated_at": wall_start + 12})

        # 15s 暂停
        conn.execute("UPDATE rooms SET status='paused', elapsed_ms=15000, last_resume_at=NULL WHERE id=?",
                     (rid,))
        conn.commit()
        e_pause = add_event(conn, rid, wall_start + 15.0, "clock",
                            {"action": "paused", "status": "paused", "elapsed_ms": 15000})

        # 20s 继续（墙钟 t0+40）：run_start 15s
        wall_resume = t0 + 40
        conn.execute("UPDATE rooms SET status='running', last_resume_at=? WHERE id=?",
                     (wall_resume, rid))
        conn.commit()
        e_resume = add_event(conn, rid, wall_resume, "clock",
                             {"action": "resumed", "status": "running", "elapsed_ms": 15000})

        # 虚拟 20s（墙钟 resume+5）揭示 c2
        conn.execute("UPDATE cards SET revealed=1, revealed_at_ms=? WHERE id=?", (20000, c2))
        conn.commit()
        e_rev2 = add_event(conn, rid, wall_resume + 5.0, "card_revealed",
                           {"id": c2, "seq": 2, "trigger_seconds": 20, "title": "定向给红队",
                            "group_id": g1, "revealed": True, "revealed_at_ms": 20000,
                            "timed_out": False, "answers_locked": False})

        # 虚拟 ~31s（resume+16）重置：c3 门槛已由 c1 回执解锁，在 30s 揭示
        conn.execute("UPDATE cards SET revealed=1, revealed_at_ms=? WHERE id=?", (30000, c3))
        conn.commit()
        e_rev3 = add_event(conn, rid, wall_resume + 15.0, "card_revealed",
                           {"id": c3, "seq": 3, "trigger_seconds": 30, "title": "门槛卡",
                            "group_id": None, "revealed": True, "revealed_at_ms": 30000,
                            "timed_out": False, "answers_locked": False})
        wall_reset = wall_resume + 16.0
        # 重置落库：清揭示/回执/答复（模拟 reset 路由效果）
        conn.execute("UPDATE cards SET revealed=0, revealed_at_ms=NULL, timed_out=0, answers_locked=0 WHERE room_id=?", (rid,))
        conn.execute("DELETE FROM receipts WHERE room_id=?", (rid,))
        conn.execute("DELETE FROM answers WHERE room_id=?", (rid,))
        conn.execute("UPDATE rooms SET status='idle', elapsed_ms=0, last_resume_at=NULL WHERE id=?", (rid,))
        conn.commit()
        e_reset = add_event(conn, rid, wall_reset, "reset",
                            {"action": "reset", "status": "idle", "elapsed_ms": 0})

        # 第二轮：开始 + c1 在 10s 再次揭示 + 红队新回执/新答复 v1
        wall2 = wall_reset + 100.0
        conn.execute("UPDATE rooms SET status='running', last_resume_at=? WHERE id=?", (wall2, rid))
        conn.commit()
        e_start2 = add_event(conn, rid, wall2, "clock",
                             {"action": "started", "status": "running", "elapsed_ms": 0})
        conn.execute("UPDATE cards SET revealed=1, revealed_at_ms=? WHERE id=?", (10000, c1))
        conn.execute("INSERT INTO receipts (room_id,card_id,group_id,created_at) VALUES (?,?,?,?)",
                     (rid, c1, g1, wall2 + 10))
        conn.execute("INSERT INTO answers (room_id,card_id,group_id,body,revision,updated_at)"
                     " VALUES (?,?,?,?,1,?)", (rid, c1, g1, "第二轮答复", wall2 + 12))
        conn.commit()
        e_rev1b = add_event(conn, rid, wall2 + 10.0, "card_revealed",
                            {"id": c1, "seq": 1, "trigger_seconds": 10, "title": "告警",
                             "group_id": None, "revealed": True, "revealed_at_ms": 10000,
                             "timed_out": False, "answers_locked": False})
        e_ack1b = add_event(conn, rid, wall2 + 10.0, "receipt_updated",
                            {"card_id": c1, "group_id": g1, "acked": True})
        e_a1b = add_event(conn, rid, wall2 + 12.0, "answer_updated",
                          {"card_id": c1, "group_id": g1, "body": "第二轮答复", "revision": 1,
                           "updated_at": wall2 + 12})

        high_water = conn.execute("SELECT MAX(id) m FROM events WHERE room_id=?", (rid,)).fetchone()["m"]
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone()
        data = rv.build_review_data(conn, room, high_water)

        # 水位字段（时间线按 (轮次, 虚拟时间, ID) 排序，非全局 ID 序；下面单独校验排序）
        assert data["high_water_event_id"] == high_water
        ids = [e["id"] for e in data["events"]]
        assert max(ids) == high_water and all(i <= high_water for i in ids)

        # 脱敏：任何事件 / 字符串都不含凭证
        raw = json.dumps(data, ensure_ascii=False)
        for secret in ("AAA111", "BBB222", "ABC234", "SECRET-TOKEN-XYZ", "join_code", "token"):
            assert secret not in raw, f"secret leaked: {secret}"
        assert all(not e["type"].startswith("share_") for e in data["events"])

        # 编排
        orch = data["orchestration"]
        assert {g["name"] for g in orch["groups"]} == {"红队", "蓝队"}
        assert all("join_code" not in g for g in orch["groups"])
        seqs = [c["seq"] for c in orch["cards"]]
        assert seqs == [1, 2, 3]
        gate_card = orch["cards"][2]
        assert gate_card["gate"]["depends_on_seq"] == 1
        assert gate_card["gate"]["gate_timeout_seconds"] == 60

        # 水位时刻（第二轮运行中）：当前状态表 —— c1 已揭示，其余未揭示
        assert {c["id"] for c in data["revealed_cards"]} == {c1}
        receipts = data["receipts"]
        assert {(r["card_id"], r["group_id"]) for r in receipts} == {(c1, g1)}
        answers = data["answers"]
        assert len(answers) == 1 and answers[0]["body"] == "第二轮答复" and answers[0]["revision"] == 1

        # 答复版本轨迹：红队 c1 共 3 个版本（v1/v2 第一轮 + v1 第二轮），按事件 ID 升序
        vers = [v for v in data["answer_versions"] if v["card_id"] == c1 and v["group_id"] == g1]
        assert [v["revision"] for v in vers] == [1, 2, 1]
        assert [v["body"] for v in vers] == ["初判：重启", "再判：扩容", "第二轮答复"]

        # 时钟动作：start/pause/resume/reset/start，轮次标注正确
        acts = [(a["action"], a["round"], a["vtime_ms"]) for a in data["clock_actions"]]
        assert acts == [
            ("started", 0, 0), ("paused", 0, 15000), ("resumed", 0, 15000),
            ("reset", 0, 31000), ("started", 1, 0),
        ], acts

        # 轮次：第 0 轮 end=31000 已结束，第 1 轮 end=运行到水位的外推值
        rounds = data["rounds"]
        assert rounds[0]["ended"] is True and rounds[0]["end_ms"] == 31000
        assert rounds[1]["ended"] is False
        assert rounds[1]["end_ms"] >= 12000  # wall2+12 起算

        # 时间线虚拟时间：同墙钟秒的 c1 揭示/两回执 vtime 都是 10000，按 ID 排序
        tl = data["events"]
        at10 = [e for e in tl if e["round"] == 0 and e["vtime_ms"] == 10000]
        assert [e["type"] for e in at10] == ["card_revealed", "receipt_updated", "receipt_updated"]
        assert [e["id"] for e in at10] == sorted(e["id"] for e in at10)
        # resume 段：c2 揭示虚拟 20000（墙钟 resume+5），c3 揭示 30000
        vtime = {e["id"]: e["vtime_ms"] for e in tl}
        assert vtime[e_rev2] == 20000, vtime[e_rev2]
        assert vtime[e_rev3] == 30000, vtime[e_rev3]
        assert vtime[e_a2] == 12000  # 第一段运行中：wall_start+12
        # 暂停期间事件挂靠冻结值 15000（此处无暂停期间业务事件，仅校验 reset 前边界）
        assert vtime[e_reset] == 31000
        # 第二轮事件 round=1，vtime 重新从 0 起算
        assert vtime[e_rev1b] == 10000
        r1 = [e for e in tl if e["round"] == 1]
        assert r1 and all(e["vtime_ms"] >= 0 for e in r1)

        # 时间线整体按 (round, vtime, id) 排序
        keys = [(e["round"], e["vtime_ms"], e["id"]) for e in tl]
        assert keys == sorted(keys)

        # ZIP + 回放页
        blob = rv.build_review_zip(data)
        with zipfile.ZipFile(__import__("io").BytesIO(blob)) as zf:
            html = zf.read("replay.html").decode("utf-8")
            assert "__REPLAY_DATA_JSON__" not in html
            assert "__REVIEW_DATA_SUMMARY__" not in html
            assert "核心库故障" in html
            assert "AAA111" not in html and "SECRET-TOKEN-XYZ" not in html
            # 数据确实内联
            assert '"format": "inject-review"' in html or '"format":"inject-review"' in html

        # 文件名安全化
        fn = rv.review_filename("核心库 故障/演练:1", 1700000000)
        assert fn.endswith(".zip") and "/" not in fn and ":" not in fn

        conn.close()
    print("ok: full two-round drill: watermark, sanitization, versions, timeline, zip")


def test_watermark_boundary():
    """水位截断：id > high_water 的事件/状态不得出现（生成期间到达不混入）。"""
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        rid = make_room(conn, "w")
        g1 = conn.execute(
            "INSERT INTO groups (room_id,name,join_code,created_at) VALUES (?,?,?,?)",
            (rid, "G", "ZZZ999", 1.0)).lastrowid
        c1 = conn.execute(
            "INSERT INTO cards (room_id,seq,trigger_seconds,title,group_id,revealed,revealed_at_ms)"
            " VALUES (?,?,?,?,?,1,5000)", (rid, 1, 5, "old", None)).lastrowid
        conn.commit()
        e1 = add_event(conn, rid, 1.0, "group_added", {"id": g1, "name": "G", "join_code": "ZZZ999"})
        e2 = add_event(conn, rid, 2.0, "card_added", {"id": c1, "seq": 1, "trigger_seconds": 5,
                                                       "title": "old", "group_id": None})
        e3 = add_event(conn, rid, 3.0, "card_revealed", {"id": c1, "seq": 1, "title": "old",
                                                          "group_id": None})
        # 水位取 e2：揭示事件 e3 与任何后续都不得进入
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone()
        data = rv.build_review_data(conn, room, e2)
        ids = [e["id"] for e in data["events"]]
        assert ids == [e1, e2]
        assert data["high_water_event_id"] == e2
        # 时间线中不存在揭示事件
        assert all(e["type"] != "card_revealed" for e in data["events"])
        conn.close()
    print("ok: watermark boundary excludes later events")


def test_anchors_export():
    """复盘锚点：按绑定事件水位过滤、跨重置归属原轮次、锚点事件不进事件流、ZIP 内含锚点。"""
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(os.path.join(d, "t.db"))
        t0 = 5000.0
        rid = make_room(conn, "锚点演练")

        g1 = conn.execute(
            "INSERT INTO groups (room_id,name,join_code,created_at) VALUES (?,?,?,?)",
            (rid, "红队", "CCC111", t0)).lastrowid
        c1 = conn.execute(
            "INSERT INTO cards (room_id,seq,trigger_seconds,title,group_id)"
            " VALUES (?,?,?,?,?)", (rid, 1, 10, "卡片", None)).lastrowid
        conn.commit()
        e_g = add_event(conn, rid, t0 + 1, "group_added",
                        {"id": g1, "name": "红队", "join_code": "CCC111"})
        e_c = add_event(conn, rid, t0 + 2, "card_added",
                        {"id": c1, "seq": 1, "trigger_seconds": 10, "title": "卡片",
                         "group_id": None})

        # 锚点 1：开始前（round 0, vtime 0, 水位 = e_c）
        a1 = conn.execute(
            "INSERT INTO anchors (room_id,round_index,vtime_ms,bound_event_id,"
            "title,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, 0, 0, e_c, "开始前检查", "确认全员就位", t0 + 3, t0 + 3)).lastrowid
        # anchor_added 事件（id 在锚点绑定水位之后）——不应进入导出事件流
        add_event(conn, rid, t0 + 3, "anchor_added",
                  {"id": a1, "round": 0, "vtime_ms": 0, "bound_event_id": e_c,
                   "title": "开始前检查"})

        wall_start = t0 + 10
        add_event(conn, rid, wall_start, "clock",
                  {"action": "started", "status": "running", "elapsed_ms": 0})
        conn.execute("UPDATE rooms SET status='running', elapsed_ms=0, last_resume_at=? WHERE id=?",
                     (wall_start, rid))
        conn.commit()
        e_rev = add_event(conn, rid, wall_start + 10.0, "card_revealed",
                          {"id": c1, "seq": 1, "trigger_seconds": 10, "title": "卡片",
                           "group_id": None, "revealed": True, "revealed_at_ms": 10000,
                           "timed_out": False, "answers_locked": False})

        # 锚点 2：运行中 10s（round 0, vtime 10000, 水位 = e_rev）
        a2 = conn.execute(
            "INSERT INTO anchors (room_id,round_index,vtime_ms,bound_event_id,"
            "title,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, 0, 10000, e_rev, "揭示瞬间", "", wall_start + 10, wall_start + 10)).lastrowid
        add_event(conn, rid, wall_start + 10.1, "anchor_added", {"id": a2})

        # 暂停于 12s
        conn.execute("UPDATE rooms SET status='paused', elapsed_ms=12000, last_resume_at=NULL WHERE id=?",
                     (rid,))
        conn.commit()
        e_pause = add_event(conn, rid, wall_start + 12.0, "clock",
                            {"action": "paused", "status": "paused", "elapsed_ms": 12000})

        # 锚点 3：暂停期间——虚拟时间冻结于 12000；之后改备注
        a3 = conn.execute(
            "INSERT INTO anchors (room_id,round_index,vtime_ms,bound_event_id,"
            "title,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, 0, 12000, e_pause, "暂停复盘", "原备注", wall_start + 30, wall_start + 30)
        ).lastrowid
        e_a3add = add_event(conn, rid, wall_start + 30, "anchor_added", {"id": a3})
        conn.execute("UPDATE anchors SET note=?, updated_at=? WHERE id=?",
                     ("修改后的备注", wall_start + 40, a3))
        conn.commit()
        add_event(conn, rid, wall_start + 40, "anchor_updated", {"id": a3, "note": "修改后的备注"})

        # 重置：锚点保留，新一轮从 0 起
        conn.execute("UPDATE cards SET revealed=0, revealed_at_ms=NULL WHERE room_id=?", (rid,))
        conn.execute("UPDATE rooms SET status='idle', elapsed_ms=0, last_resume_at=NULL WHERE id=?",
                     (rid,))
        conn.commit()
        e_reset = add_event(conn, rid, wall_start + 50, "reset",
                            {"action": "reset", "status": "idle", "elapsed_ms": 0})

        # 锚点 4：重置后（round 1, vtime 0），绑定水位 = e_reset
        a4 = conn.execute(
            "INSERT INTO anchors (room_id,round_index,vtime_ms,bound_event_id,"
            "title,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, 1, 0, e_reset, "新一轮准备", "", wall_start + 60, wall_start + 60)).lastrowid
        e_a4add = add_event(conn, rid, wall_start + 60, "anchor_added", {"id": a4})

        # 锚点 5：未来锚点——绑定水位晚于即将使用的导出水位，导出时必须被排除
        a5 = conn.execute(
            "INSERT INTO anchors (room_id,round_index,vtime_ms,bound_event_id,"
            "title,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, 1, 0, e_a4add, "不该进包", "", wall_start + 70, wall_start + 70)).lastrowid
        add_event(conn, rid, wall_start + 70, "anchor_added", {"id": a5})

        # 导出水位卡在 e_a3add：锚点 1/2/3 入包；锚点 4（bound=e_reset > 水位）、
        # 锚点 5（bound=e_a4add > 水位）均在水位之后记录，必须排除
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone()
        data = rv.build_review_data(conn, room, e_a3add)

        anchors = data["anchors"]
        by_id = {a["id"]: a for a in anchors}
        assert set(by_id) == {a1, a2, a3}, set(by_id)
        assert a4 not in by_id and a5 not in by_id
        # 顺序：(round, vtime, id) —— 库内即按此序返回
        assert [a["id"] for a in anchors] == [a1, a2, a3]
        assert by_id[a1]["round"] == 0 and by_id[a1]["vtime_ms"] == 0
        assert by_id[a2]["round"] == 0 and by_id[a2]["vtime_ms"] == 10000
        assert by_id[a3]["round"] == 0 and by_id[a3]["vtime_ms"] == 12000
        assert all(a["bound_event_id"] <= e_a3add for a in anchors)
        # 备注为导出时刻最新值
        assert by_id[a3]["note"] == "修改后的备注"

        # anchor_* 事件整类不进脱敏事件流
        assert all(not e["type"].startswith("anchor_") for e in data["events"])
        # 脱敏：组加入码不泄露
        assert "CCC111" not in json.dumps(data, ensure_ascii=False)

        # ZIP 内 data.json 与 replay.html 都含锚点（且未来锚点不出现）
        blob = rv.build_review_zip(data)
        with zipfile.ZipFile(__import__("io").BytesIO(blob)) as zf:
            dj = json.loads(zf.read("data.json").decode("utf-8"))
            assert {a["id"] for a in dj["anchors"]} == {a1, a2, a3}
            html = zf.read("replay.html").decode("utf-8")
            assert "__REPLAY_DATA_JSON__" not in html
            assert "暂停复盘" in html and "揭示瞬间" in html
            assert "新一轮准备" not in html and "不该进包" not in html

        # 水位取全局最大：未来锚点同样出现（不晚于导出水位即可）
        high = conn.execute("SELECT MAX(id) m FROM events WHERE room_id=?", (rid,)).fetchone()["m"]
        data2 = rv.build_review_data(conn, room, high)
        assert {a["id"] for a in data2["anchors"]} == {a1, a2, a3, a4, a5}
        conn.close()
    print("ok: anchors export: watermark filter, round binding, frozen vtime, zip")


def test_old_db_migration_creates_anchors():
    """旧库（无 anchors 表）启动迁移后 anchors 表自动补建，既有数据完好。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.db")
        old = sqlite3.connect(path)
        # 模拟最早版本的精简结构：rooms/groups/cards/events，无任何新列新表
        old.executescript(
            """
            CREATE TABLE rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idle',
                elapsed_ms INTEGER NOT NULL DEFAULT 0, last_resume_at REAL,
                created_at REAL NOT NULL);
            CREATE TABLE groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL,
                name TEXT NOT NULL, join_code TEXT NOT NULL UNIQUE, created_at REAL NOT NULL);
            CREATE TABLE cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL,
                seq INTEGER NOT NULL, trigger_seconds INTEGER NOT NULL,
                title TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                revealed INTEGER NOT NULL DEFAULT 0, revealed_at_ms INTEGER);
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL,
                type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
            """
        )
        old.execute(
            "INSERT INTO rooms (code,name,status,elapsed_ms,created_at) VALUES (?,?,?,?,?)",
            ("OLD001", "旧库房间", "paused", 9000, 1.0))
        old.commit()
        old.close()

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        dbmod.init_schema(conn)  # 迁移 + 建表 + running 冻结（本库无 running）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cards)")}
        for needed in ("group_id", "depends_on_card_id", "gate_timeout_seconds",
                       "timed_out", "answers_locked"):
            assert needed in cols, needed
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for needed in ("anchors", "answers", "observer_shares", "receipts"):
            assert needed in names, needed
        # 既有数据完好，且可直接写入锚点
        room = conn.execute("SELECT * FROM rooms WHERE code='OLD001'").fetchone()
        assert room["name"] == "旧库房间" and room["elapsed_ms"] == 9000
        conn.execute(
            "INSERT INTO anchors (room_id,round_index,vtime_ms,bound_event_id,"
            "title,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (room["id"], 0, 9000, 0, "迁移后锚点", "", 2.0, 2.0))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) c FROM anchors").fetchone()["c"] == 1
        conn.close()
    print("ok: old database migration creates anchors table")


if __name__ == "__main__":
    test_empty_drill()
    test_full_drill()
    test_watermark_boundary()
    test_anchors_export()
    test_old_db_migration_creates_anchors()
    print("ALL EXPORT TESTS PASSED")
