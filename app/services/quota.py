"""Đếm quota dịch vụ ngoài. Không có cái này thì hết quota mà không biết."""

from __future__ import annotations

from datetime import UTC, datetime

from app import db

LIMITS = {"scrapedo": 1000, "gemini": 1500}  # free tier


def period(service: str) -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m") if service == "scrapedo" else now.strftime("%Y-%m-%d")


async def bump(service: str, n: int = 1) -> None:
    await db.execute(
        "INSERT INTO quota (service, period, used) VALUES (?,?,?) "
        "ON CONFLICT(service, period) DO UPDATE SET used = quota.used + excluded.used",
        (service, period(service), n),
    )


async def set_remaining(service: str, remaining: int) -> None:
    """Nhà cung cấp trả số credit CÒN LẠI -> quy ra đã dùng. Chính xác hơn tự đếm,
    vì nó tính cả request bị tính phí mà mình không thấy (404 upstream, retry nội bộ)."""
    limit = LIMITS.get(service, 0)
    await db.execute(
        "INSERT INTO quota (service, period, used) VALUES (?,?,?) "
        "ON CONFLICT(service, period) DO UPDATE SET used = GREATEST(quota.used, excluded.used)",
        (service, period(service), max(0, limit - remaining)),
    )


async def used(service: str) -> int:
    row = await db.fetch_one(
        "SELECT used FROM quota WHERE service=? AND period=?", (service, period(service))
    )
    return int(row["used"]) if row else 0


async def snapshot() -> list[dict]:
    out = []
    for svc, limit in LIMITS.items():
        u = await used(svc)
        out.append({
            "service": svc,
            "used": u,
            "limit": limit,
            "pct": min(100, round(u / limit * 100)) if limit else 0,
            "window": "tháng" if svc == "scrapedo" else "ngày",
        })
    return out
