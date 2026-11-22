from __future__ import annotations

from fastapi import APIRouter

from app.db import connect
from app.services.gate import today_str

router = APIRouter(tags=["总览"])


@router.get("/总览")
def dashboard():
    conn = connect()
    try:
        def cnt(status: str) -> int:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM hang_tickets WHERE status=?", (status,)
            ).fetchone()["c"]

        day = today_str()
        gate = conn.execute("SELECT * FROM day_gates WHERE day=?", (day,)).fetchone()
        wm = conn.execute(
            "SELECT value FROM watermarks WHERE key='在途票'"
        ).fetchone()
        # 未解决隔离：以隔离表未关行为准（架构 A 下丢失行永不关闭，恒计入）
        unresolved = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantines WHERE resolved=0"
        ).fetchone()["c"]
        unresolved_lost = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantines WHERE resolved=0 AND kind='丢失隔离'"
        ).fetchone()["c"]
        return {
            "今日": day,
            "日闸": "已结案" if gate and gate["sealed"] else "开放",
            "在途水位": wm["value"] if wm else 0,
            "隔离未解决": unresolved,
            "丢失隔离未解决": unresolved_lost,
            "已收": cnt("已收"),
            "水洗": cnt("水洗"),
            "烘干": cnt("烘干"),
            "熨烫": cnt("熨烫"),
            "待取": cnt("待取"),
            "已取": cnt("已取"),
            "超期隔离": cnt("超期隔离"),
            "丢失隔离": cnt("丢失隔离"),
        }
    finally:
        conn.close()
