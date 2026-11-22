from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from app.db import (
    CLOSED_STATUSES,
    FORWARD,
    LOST_STATUS,
    QUARANTINE_KINDS,
    connect,
    transaction,
)
from app.services.gate import ensure_day_row, is_day_sealed, refresh_in_flight, today_str


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _add_event(conn, ticket_no: str, from_status, to_status: str, operator: str, note: str) -> None:
    conn.execute(
        """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
           VALUES (?,?,?,?,?,?)""",
        (ticket_no, from_status, to_status, operator or "店员", _now(), note or ""),
    )


def _open_quarantine(conn, ticket_no: str) -> object | None:
    return conn.execute(
        "SELECT * FROM quarantines WHERE ticket_no=? AND resolved=0 ORDER BY id DESC LIMIT 1",
        (ticket_no,),
    ).fetchone()


def _close_open_quarantine(conn, ticket_no: str, note: str) -> None:
    """关闭该挂牌全部未关隔离行。与挂牌态写在同一事务，杜绝「票态变了隔离行还开着」。"""
    conn.execute(
        """UPDATE quarantines
           SET resolved=1, resolved_at=?, resolve_note=?
           WHERE ticket_no=? AND resolved=0""",
        (_now(), note, ticket_no),
    )


def list_tickets(status: str | None = None, store_code: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM hang_tickets WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if store_code:
            sql += " AND store_code=?"
            args.append(store_code)
        sql += " ORDER BY id DESC"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        for r in rows:
            r["items"] = [
                dict(i)
                for i in conn.execute(
                    "SELECT * FROM ticket_items WHERE ticket_no=? ORDER BY id",
                    (r["ticket_no"],),
                ).fetchall()
            ]
        return rows
    finally:
        conn.close()


def get_ticket(ticket_no: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        data = dict(row)
        data["items"] = [
            dict(i)
            for i in conn.execute(
                "SELECT * FROM ticket_items WHERE ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        data["events"] = [
            dict(e)
            for e in conn.execute(
                "SELECT * FROM process_events WHERE ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        data["quarantines"] = [
            dict(q)
            for q in conn.execute(
                "SELECT * FROM quarantines WHERE ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        data["claims"] = [
            dict(c)
            for c in conn.execute(
                "SELECT * FROM claims WHERE ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        return data
    finally:
        conn.close()


def create_ticket(payload: dict) -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止新开挂牌")
    ticket_no = (payload.get("ticket_no") or "").strip()
    store_code = (payload.get("store_code") or "").strip()
    pickup_code = (payload.get("pickup_code") or "").strip()
    customer_name = (payload.get("customer_name") or "").strip()
    due_date = (payload.get("due_date") or "").strip() or today_str()
    note = (payload.get("note") or "").strip()
    items = payload.get("items") or []
    if not ticket_no or not store_code or not pickup_code or not customer_name:
        raise HTTPException(400, "票号、门店、暗码、顾客名必填")
    if not items:
        raise HTTPException(400, "至少一件衣物")

    with transaction() as conn:
        ensure_day_row(conn)
        store = conn.execute(
            "SELECT * FROM stores WHERE code=? AND active=1", (store_code,)
        ).fetchone()
        if not store:
            raise HTTPException(400, "门店不可用")
        exists = conn.execute(
            "SELECT 1 FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if exists:
            raise HTTPException(409, "票号已存在")
        conn.execute(
            """INSERT INTO hang_tickets
               (ticket_no, store_code, pickup_code, customer_name, status, opened_at, due_date, note)
               VALUES (?,?,?,?, '已收',?,?,?)""",
            (ticket_no, store_code, pickup_code, customer_name, _now(), due_date, note),
        )
        for it in items:
            garment = (it.get("garment") or "").strip()
            qty = int(it.get("qty") or 0)
            if not garment or qty <= 0:
                raise HTTPException(400, "衣物件不合法")
            conn.execute(
                "INSERT INTO ticket_items(ticket_no, garment, qty, remark) VALUES (?,?,?,?)",
                (ticket_no, garment, qty, (it.get("remark") or "").strip()),
            )
        _add_event(conn, ticket_no, None, "已收", payload.get("operator") or "店员", "收衣开票")
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def advance(ticket_no: str, to_status: str, operator: str = "店员", note: str = "") -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止改工序")
    to_status = (to_status or "").strip()
    with transaction() as conn:
        ensure_day_row(conn)
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        cur = row["status"]
        # 架构 A：丢失为终态，推进一律硬拒（已取/超期隔离同样不可推进）
        if cur == LOST_STATUS:
            raise HTTPException(409, f"票籍已「{LOST_STATUS}」终态，禁止推进工序")
        if cur in CLOSED_STATUSES or cur == "超期隔离":
            raise HTTPException(409, f"当前态「{cur}」不可推进工序")
        allowed = FORWARD.get(cur, ())
        if to_status not in allowed:
            raise HTTPException(409, f"不可从「{cur}」到「{to_status}」")
        conn.execute(
            "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
            (to_status, ticket_no),
        )
        _add_event(conn, ticket_no, cur, to_status, operator, note)
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def pickup(ticket_no: str, pickup_code: str, operator: str = "店员") -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止取衣核销")
    with transaction() as conn:
        ensure_day_row(conn)
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        # 架构 A：丢失终态下即使暗码正确也必须硬拒，杜绝「丢失票仍能暗码取衣」
        if row["status"] == LOST_STATUS:
            raise HTTPException(409, f"票籍已「{LOST_STATUS}」终态，禁止取衣核销")
        if row["status"] != "待取":
            raise HTTPException(409, f"仅「待取」可核销，当前「{row['status']}」")
        if (pickup_code or "").strip() != row["pickup_code"]:
            raise HTTPException(403, "取衣暗码不符")
        now = _now()
        conn.execute(
            "UPDATE hang_tickets SET status='已取', closed_at=? WHERE ticket_no=?",
            (now, ticket_no),
        )
        conn.execute(
            """INSERT INTO pickups(ticket_no, pickup_code, picked_at, operator)
               VALUES (?,?,?,?)""",
            (ticket_no, row["pickup_code"], now, operator or "店员"),
        )
        _add_event(conn, ticket_no, "待取", "已取", operator, "取衣核销")
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def quarantine(ticket_no: str, kind: str, reason: str, operator: str = "店员") -> dict:
    kind = (kind or "").strip()
    if kind not in QUARANTINE_KINDS:
        raise HTTPException(400, "隔离种类仅超期隔离或丢失隔离")
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        cur = row["status"]
        # 架构 A：丢失是终态——已在丢失隔离的票禁止再隔离（含重复丢失/再超期）
        if cur == LOST_STATUS:
            raise HTTPException(409, f"票籍已「{LOST_STATUS}」终态，禁止再隔离")
        if cur == "已取":
            raise HTTPException(409, "当前态「已取」不可隔离")
        if kind == "超期隔离" and cur not in ("待取", "超期隔离"):
            raise HTTPException(409, "超期隔离仅从待取进入")

        existing = _open_quarantine(conn, ticket_no)
        # 同种类且已有未关隔离行：幂等拒绝，避免重复开放行造成隔离表与票态分叉
        if existing and existing["kind"] == kind:
            raise HTTPException(409, f"该挂牌已存在未关闭的「{kind}」行，请勿重复隔离")

        now = _now()
        # 不同种类切换（超期隔离 → 丢失隔离）：先原子关闭旧的未关行，再开新行，
        # 保证任一挂牌至多一行未关隔离，隔离表与挂牌态始终同成败。
        if existing:
            _close_open_quarantine(conn, ticket_no, f"转{kind}，关闭原隔离行")
        conn.execute(
            "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
            (kind, ticket_no),
        )
        conn.execute(
            """INSERT INTO quarantines(ticket_no, kind, reason, created_at, resolved)
               VALUES (?,?,?,?,0)""",
            (ticket_no, kind, reason or kind, now),
        )
        _add_event(conn, ticket_no, cur, kind, operator, reason or "")
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def claim_lost_ticket(payload: dict) -> dict:
    """
    架构 A 的「认领」：丢失为终态，认领只允许留审计痕迹。
    - 不回改 hang_tickets.status（仍是丢失隔离）
    - 不关闭 quarantines 丢失隔离行（resolved 保持 0）
    - 仅追加 claims 审计行与一条「认领登记」事件
    因此本操作不存在「半截认领」：它不触碰任何终态字段。
    """
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止认领登记")
    ticket_no = (payload.get("ticket_no") or "").strip()
    claimant = (payload.get("claimant") or "").strip()
    if not ticket_no:
        raise HTTPException(400, "票号必填")
    if not claimant:
        raise HTTPException(400, "认领人必填（认领须附认领人）")
    contact = (payload.get("contact") or "").strip()
    evidence = (payload.get("evidence") or "").strip()
    note = (payload.get("note") or "").strip()
    operator = (payload.get("operator") or "店员").strip()

    with transaction() as conn:
        ensure_day_row(conn)
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        if row["status"] != LOST_STATUS:
            raise HTTPException(
                409, f"仅「{LOST_STATUS}」票可登记认领，当前「{row['status']}」"
            )
        now = _now()
        conn.execute(
            """INSERT INTO claims(ticket_no, claimant, contact, evidence, note, operator, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (ticket_no, claimant, contact, evidence, note, operator, now),
        )
        # 事件 to_status 与现态相同，表达「只登记、不改态」，事件流可重放且不产生状态跳变
        _add_event(
            conn,
            ticket_no,
            LOST_STATUS,
            LOST_STATUS,
            operator,
            f"认领登记：{claimant}" + (f"／凭证：{evidence}" if evidence else ""),
        )
        # 认领不改票籍/不开关隔离行，水位本就不变；仍刷新一次保持口径一致
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def list_claims(ticket_no: str | None = None) -> list[dict]:
    conn = connect()
    try:
        if ticket_no:
            rows = conn.execute(
                "SELECT * FROM claims WHERE ticket_no=? ORDER BY id DESC", (ticket_no,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM claims ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
