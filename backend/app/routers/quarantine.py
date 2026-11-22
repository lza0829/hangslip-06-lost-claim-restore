from __future__ import annotations

from fastapi import APIRouter

from app.db import connect

router = APIRouter(tags=["隔离"])


@router.get("/隔离")
def list_quarantine():
    """隔离流水联挂当前挂牌态，便于页面互证「隔离行开关 ↔ 票态」不漂移。"""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT q.id, q.ticket_no, q.kind, q.reason, q.created_at,
                   q.resolved, q.resolved_at, q.resolve_note,
                   t.status AS ticket_status,
                   t.customer_name AS customer_name
            FROM quarantines q
            JOIN hang_tickets t ON t.ticket_no = q.ticket_no
            ORDER BY q.id DESC
            """
        ).fetchall()
        data = [dict(r) for r in rows]
        for r in data:
            r["状态"] = "未解决" if r["resolved"] == 0 else "已关闭"
        return data
    finally:
        conn.close()


@router.get("/隔离/未解决数")
def unresolved_count():
    """隔离表口径的未解决数：与总览同源，任一未关隔离行计 1。"""
    conn = connect()
    try:
        c = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantines WHERE resolved=0"
        ).fetchone()["c"]
        # 按种类拆分，便于总览核对丢失/超期
        per_kind = {
            r["kind"]: r["c"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS c FROM quarantines WHERE resolved=0 GROUP BY kind"
            ).fetchall()
        }
        return {
            "未解决数": c,
            "丢失隔离未解决": per_kind.get("丢失隔离", 0),
            "超期隔离未解决": per_kind.get("超期隔离", 0),
        }
    finally:
        conn.close()
