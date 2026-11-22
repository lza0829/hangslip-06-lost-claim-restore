"""
架构 A（丢失即终态）一致性与并发测试。
只覆盖「丢失后恢复」口径，不涉及超期自动扫描。

运行：cd backend && python3 -m unittest tests.test_loss_terminal -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class LossTerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例独立临时库，隔离运行
        self._tmp = tempfile.TemporaryDirectory()
        from app import config

        self._config = config
        config.DB_PATH = config.Path(self._tmp.name) / "hangslip.db"
        from app import db

        self._db = db
        db.init_db()
        from app.services import tickets as svc
        from app.services import gate as gate_svc

        self.svc = svc
        self.gate = gate_svc
        self.conn = db.connect()
        # 准备门店与日闸
        self.conn.execute(
            "INSERT INTO stores(code,name,rack_count,active) VALUES('店-测','测试店',2,1)"
        )
        self.conn.execute(
            "INSERT INTO day_gates(day,sealed,sealed_at,note) VALUES(?,0,NULL,'')",
            (date.today().isoformat(),),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # ---- helpers ----
    def _make(self, no: str, status: str = "待取") -> None:
        now = "2026-01-01 09:00:00"
        due = (date.today() + timedelta(days=1)).isoformat()
        self.conn.execute(
            """INSERT INTO hang_tickets
               (ticket_no,store_code,pickup_code,customer_name,status,opened_at,due_date)
               VALUES(?,?,?,?,?,?,?)""",
            (no, "店-测", f"码-{no}", "顾客", status, now, due),
        )
        self.conn.execute(
            "INSERT INTO ticket_items(ticket_no,garment,qty) VALUES(?,?,?)",
            (no, "衬衣", 1),
        )
        self.conn.commit()

    def _status(self, no: str) -> str:
        return self.conn.execute(
            "SELECT status FROM hang_tickets WHERE ticket_no=?", (no,)
        ).fetchone()["status"]

    def _open_qrows(self, no: str):
        return self.conn.execute(
            "SELECT * FROM quarantines WHERE ticket_no=? AND resolved=0", (no,)
        ).fetchall()

    def _lose(self, no: str) -> None:
        self.svc.quarantine(no, "丢失隔离", "店内申报丢失", "店长")

    def _assert_http(self, fn, code: int):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            fn()
        self.assertEqual(ctx.exception.status_code, code)

    # ---- 1. 丢失是终态：取衣/推进/再隔离一律硬拒（暗码对也拒） ----
    def test_lost_ticket_pickup_hard_rejected_even_with_right_code(self):
        no = "挂-T1"
        self._make(no)
        self._lose(no)
        self.assertEqual(self._status(no), "丢失隔离")
        # 暗码完全正确，仍必须 409
        self._assert_http(lambda: self.svc.pickup(no, f"码-{no}", "柜员"), 409)
        # 暗码错误同样 409（终态判断先于暗码，不泄露暗码对错）
        self._assert_http(lambda: self.svc.pickup(no, "错码", "柜员"), 409)
        self.assertEqual(self._status(no), "丢失隔离")
        # 没有产生任何取衣核销记录
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM pickups WHERE ticket_no=?", (no,)
        ).fetchone()["c"]
        self.assertEqual(n, 0)

    def test_lost_ticket_advance_hard_rejected(self):
        no = "挂-T2"
        self._make(no, "水洗")
        self._lose(no)
        self._assert_http(lambda: self.svc.advance(no, "烘干", "工序员"), 409)
        self.assertEqual(self._status(no), "丢失隔离")

    def test_lost_ticket_re_quarantine_hard_rejected(self):
        no = "挂-T3"
        self._make(no)
        self._lose(no)
        # 已丢失后再报丢失、再报超期都必须拒绝
        self._assert_http(
            lambda: self.svc.quarantine(no, "丢失隔离", "重复丢失", "店长"), 409
        )
        self._assert_http(
            lambda: self.svc.quarantine(no, "超期隔离", "再超期", "店长"), 409
        )
        self.assertEqual(len(self._open_qrows(no)), 1)

    # ---- 2. 认领只留审计：不改态、不关隔离行 ----
    def test_claim_only_audits_no_state_change(self):
        no = "挂-T4"
        self._make(no)
        self._lose(no)
        before = self._status(no)
        result = self.svc.claim_lost_ticket(
            {
                "ticket_no": no,
                "claimant": "周小姐",
                "contact": "13800000000",
                "evidence": "付款截图",
                "note": "声称衣服是她的",
                "operator": "店长",
            }
        )
        # 票态不变
        self.assertEqual(self._status(no), before)
        self.assertEqual(result["status"], "丢失隔离")
        # 丢失隔离行仍未关闭
        qrows = self._open_qrows(no)
        self.assertEqual(len(qrows), 1)
        self.assertEqual(qrows[0]["kind"], "丢失隔离")
        # claims 留痕
        claims = self.conn.execute(
            "SELECT * FROM claims WHERE ticket_no=?", (no,)
        ).fetchall()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["claimant"], "周小姐")
        self.assertEqual(claims[0]["evidence"], "付款截图")
        # 事件：最后一条是认领登记，且 to_status 仍为丢失隔离（无状态跳变）
        ev = self.conn.execute(
            "SELECT * FROM process_events WHERE ticket_no=? ORDER BY id DESC LIMIT 1",
            (no,),
        ).fetchone()
        self.assertEqual(ev["to_status"], "丢失隔离")
        self.assertEqual(ev["from_status"], "丢失隔离")
        self.assertIn("认领登记", ev["note"])

    def test_claim_requires_claimant(self):
        no = "挂-T5"
        self._make(no)
        self._lose(no)
        self._assert_http(
            lambda: self.svc.claim_lost_ticket({"ticket_no": no, "claimant": "  "}),
            400,
        )

    def test_claim_rejected_for_non_lost_ticket(self):
        no = "挂-T6"
        self._make(no, "待取")
        # 未丢失的票不能走丢失认领
        self._assert_http(
            lambda: self.svc.claim_lost_ticket(
                {"ticket_no": no, "claimant": "某人"}
            ),
            409,
        )

    def test_repeated_claims_keep_all_audit_but_never_change_state(self):
        no = "挂-T7"
        self._make(no)
        self._lose(no)
        for i in range(3):
            self.svc.claim_lost_ticket(
                {"ticket_no": no, "claimant": f"认领人{i}"}
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM claims WHERE ticket_no=?", (no,)
            ).fetchone()["c"],
            3,
        )
        self.assertEqual(self._status(no), "丢失隔离")
        self.assertEqual(len(self._open_qrows(no)), 1)

    # ---- 3. 隔离表与挂牌态联动：切换种类原子关旧开新，至多一行未关 ----
    def test_switch_overdue_to_lost_closes_old_row_atomically(self):
        no = "挂-T8"
        self._make(no, "待取")
        self.svc.quarantine(no, "超期隔离", "逾承诺日", "店长")
        self.assertEqual(len(self._open_qrows(no)), 1)
        self.assertEqual(self._status(no), "超期隔离")
        # 转丢失：旧超期行关闭，新开丢失行
        self._lose(no)
        open_rows = self._open_qrows(no)
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["kind"], "丢失隔离")
        closed = self.conn.execute(
            "SELECT * FROM quarantines WHERE ticket_no=? AND resolved=1", (no,)
        ).fetchall()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["kind"], "超期隔离")
        self.assertEqual(self._status(no), "丢失隔离")

    def test_duplicate_same_kind_quarantine_rejected(self):
        no = "挂-T9"
        self._make(no, "待取")
        self.svc.quarantine(no, "超期隔离", "逾承诺日", "店长")
        self._assert_http(
            lambda: self.svc.quarantine(no, "超期隔离", "再来一次", "店长"), 409
        )
        self.assertEqual(len(self._open_qrows(no)), 1)

    def test_db_partial_unique_index_guards_single_open_row(self):
        # 库层兜底：直接插第二条未关行应被部分唯一索引拒绝
        no = "挂-T10"
        self._make(no)
        self._lose(no)
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO quarantines(ticket_no,kind,reason,created_at,resolved)
                   VALUES(?,?,?,?,0)""",
                (no, "丢失隔离", "绕过业务的脏行", "2026-01-01 10:00:00"),
            )

    # ---- 4. 失败回滚：注入故障不得留下半截态 ----
    def test_claim_rolls_back_on_failure(self):
        no = "挂-T11"
        self._make(no)
        self._lose(no)
        orig = self.svc.refresh_in_flight

        def boom(conn):
            raise RuntimeError("模拟水位刷新失败")

        self.svc.refresh_in_flight = boom
        try:
            with self.assertRaises(RuntimeError):
                self.svc.claim_lost_ticket({"ticket_no": no, "claimant": "故障人"})
        finally:
            self.svc.refresh_in_flight = orig
        # 回滚后：无 claims 残留、无事件残留、票态/隔离行不变
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM claims WHERE ticket_no=?", (no,)
            ).fetchone()["c"],
            0,
        )
        ev_cnt = self.conn.execute(
            "SELECT COUNT(*) c FROM process_events WHERE ticket_no=? AND note LIKE '%认领登记%'",
            (no,),
        ).fetchone()["c"]
        self.assertEqual(ev_cnt, 0)
        self.assertEqual(self._status(no), "丢失隔离")
        self.assertEqual(len(self._open_qrows(no)), 1)

    def test_quarantine_rolls_back_on_failure(self):
        no = "挂-T12"
        self._make(no, "待取")
        orig = self.svc.refresh_in_flight

        def boom(conn):
            raise RuntimeError("模拟水位刷新失败")

        self.svc.refresh_in_flight = boom
        try:
            with self.assertRaises(RuntimeError):
                self.svc.quarantine(no, "丢失隔离", "应回滚", "店长")
        finally:
            self.svc.refresh_in_flight = orig
        # 票态仍是待取，无隔离行、无事件
        self.assertEqual(self._status(no), "待取")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM quarantines WHERE ticket_no=?", (no,)
            ).fetchone()["c"],
            0,
        )

    # ---- 5. 并发：取衣与取衣争用不得双终态（唯一核销 + 状态条件） ----
    def test_concurrent_pickup_only_one_succeeds(self):
        no = "挂-T13"
        self._make(no, "待取")
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker():
            try:
                self.svc.pickup(no, f"码-{no}", "柜员")
                with lock:
                    outcomes.append("ok")
            except Exception as e:  # noqa: BLE001
                with lock:
                    outcomes.append(f"fail:{type(e).__name__}")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [o for o in outcomes if o == "ok"]
        self.assertEqual(len(oks), 1, f"应仅一次成功，实际：{outcomes}")
        self.assertEqual(self._status(no), "已取")
        # pickups 表对 ticket_no 唯一，至多一条
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM pickups WHERE ticket_no=?", (no,)
        ).fetchone()["c"]
        self.assertEqual(n, 1)

    # ---- 6. 并发：认领与取衣争用，不得出现双终态/矛盾态 ----
    def test_concurrent_claim_and_pickup_never_corrupts(self):
        no = "挂-T14"
        self._make(no, "待取")
        results = {"pickup_ok": 0, "claim_ok": 0, "claim_rej": 0, "pickup_rej": 0}
        lock = threading.Lock()

        def do_pickup():
            try:
                self.svc.pickup(no, f"码-{no}", "柜员")
                with lock:
                    results["pickup_ok"] += 1
            except Exception:  # noqa: BLE001
                with lock:
                    results["pickup_rej"] += 1

        def do_lose_then_claim():
            # 先尝试把票置丢失（与取衣争用），再认领；二者只能有一条终态路径成立
            try:
                self.svc.quarantine(no, "丢失隔离", "并发丢失", "店长")
            except Exception:  # noqa: BLE001
                pass
            try:
                self.svc.claim_lost_ticket({"ticket_no": no, "claimant": "并发认领人"})
                with lock:
                    results["claim_ok"] += 1
            except Exception:  # noqa: BLE001
                with lock:
                    results["claim_rej"] += 1

        t1 = threading.Thread(target=do_pickup)
        t2 = threading.Thread(target=do_lose_then_claim)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        final = self._status(no)
        # 终态只能是 已取 或 丢失隔离 之一，绝不允许矛盾
        self.assertIn(final, ("已取", "丢失隔离"))
        if final == "已取":
            # 走了取衣：取衣恰好一次，认领必然因非丢失态被拒
            self.assertEqual(results["pickup_ok"], 1)
            self.assertEqual(results["claim_ok"], 0)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) c FROM claims WHERE ticket_no=?", (no,)
                ).fetchone()["c"],
                0,
            )
        else:
            # 走了丢失：取衣必然失败，认领恰好成功一次且未改终态
            self.assertEqual(results["pickup_ok"], 0)
            self.assertEqual(results["claim_ok"], 1)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) c FROM pickups WHERE ticket_no=?", (no,)
                ).fetchone()["c"],
                0,
            )
            self.assertEqual(len(self._open_qrows(no)), 1)

    # ---- 7. 水位：丢失视为出途；总览未解决数与隔离表同源 ----
    def test_watermark_treats_lost_as_out_of_flight(self):
        self._make("挂-W1", "待取")
        self._make("挂-W2", "水洗")
        conn = self._db.connect()
        try:
            n1 = self.gate.refresh_in_flight(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(n1, 2)
        self._lose("挂-W1")  # 一张丢失出途
        conn = self._db.connect()
        try:
            n2 = conn.execute(
                "SELECT value FROM watermarks WHERE key='在途票'"
            ).fetchone()["value"]
        finally:
            conn.close()
        self.assertEqual(n2, 1)
        # 认领不改水位
        self.svc.claim_lost_ticket({"ticket_no": "挂-W1", "claimant": "某人"})
        conn = self._db.connect()
        try:
            n3 = conn.execute(
                "SELECT value FROM watermarks WHERE key='在途票'"
            ).fetchone()["value"]
        finally:
            conn.close()
        self.assertEqual(n3, 1)

    def test_dashboard_unresolved_matches_quarantine_table(self):
        from app.routers.quarantine import unresolved_count

        self._make("挂-D1", "待取")
        self._make("挂-D2", "待取")
        self._lose("挂-D1")  # 丢失：未解决且永不关闭
        self.svc.quarantine("挂-D2", "超期隔离", "逾时", "店长")
        stats = unresolved_count()
        self.assertEqual(stats["未解决数"], 2)
        self.assertEqual(stats["丢失隔离未解决"], 1)
        self.assertEqual(stats["超期隔离未解决"], 1)
        # 认领不改变未解决数
        self.svc.claim_lost_ticket({"ticket_no": "挂-D1", "claimant": "某人"})
        stats2 = unresolved_count()
        self.assertEqual(stats2["未解决数"], 2)
        self.assertEqual(stats2["丢失隔离未解决"], 1)

    # ---- 8. 全链路事件可重放：从事件流重建状态，丢失后无恢复跳变 ----
    def test_event_stream_replay_no_restore_transition(self):
        no = "挂-R1"
        self._make(no, "已收")
        self.svc.advance(no, "水洗", "工序员")
        self.svc.advance(no, "待取", "工序员")
        self._lose(no)
        self.svc.claim_lost_ticket({"ticket_no": no, "claimant": "认领人"})
        evs = self.conn.execute(
            "SELECT from_status, to_status FROM process_events WHERE ticket_no=? ORDER BY id",
            (no,),
        ).fetchall()
        # 重放：以每条 to_status 递进；认领事件 from==to 不产生回退
        state = None
        for e in evs:
            state = e["to_status"]
        self.assertEqual(state, "丢失隔离")
        # 不存在任何「丢失隔离 -> 待取/其他」的恢复边
        for e in evs:
            if e["from_status"] == "丢失隔离":
                self.assertEqual(e["to_status"], "丢失隔离")


if __name__ == "__main__":
    unittest.main(verbosity=2)
