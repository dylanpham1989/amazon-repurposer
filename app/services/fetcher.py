"""Lấy HTML Amazon — 2 tầng + cache.

Tầng 1 (trực tiếp) ĐO 2026-09-07 chỉ pass ~20-25% từ IP dân dụng: Amazon trả HTTP 200
kèm trang CAPTCHA ~3.8KB. Cookie warmup / xoay UA / client mới đều KHÔNG cứu được.
Vẫn giữ vì fail nhanh (~1s) và khi pass thì tiết kiệm 1 credit Scrape.do.

Tầng 2 dùng Scrape.do chế độ THƯỜNG (1 credit). Đã kiểm chứng là đủ cho Amazon —
KHÔNG bật super=true, nó tốn 5-25x credit cho cùng kết quả.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from app import db
from app.config import settings
from app.models import ProductData
from app.services import parser, quota, urls

MAX_CACHE_BYTES = 2_000_000
SCRAPEDO_ENDPOINT = "https://api.scrape.do/"

UA_POOL = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
)

_last_request = 0.0
_lock = asyncio.Lock()


class FetchError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


async def _throttle() -> None:
    """Tự rate-limit. Hammer nhanh từ IP nhà = mất luôn tầng 1."""
    global _last_request
    async with _lock:
        wait = settings.request_delay_seconds - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()


# ------------------------------------------------------------------ cache
def _cache_key(asin: str, marketplace: str) -> str:
    return f"{marketplace}:{asin}"


async def cache_get(asin: str, marketplace: str) -> tuple[str, str] | None:
    row = await db.fetch_one(
        "SELECT html, via, fetched_at FROM html_cache WHERE key=?",
        (_cache_key(asin, marketplace),),
    )
    if not row:
        return None
    fetched = row["fetched_at"]           # asyncpg trả datetime có tzinfo sẵn
    if datetime.now(UTC) - fetched > timedelta(hours=settings.html_cache_hours):
        return None
    return row["html"], row["via"]


async def cache_put(asin: str, marketplace: str, html: str, via: str) -> None:
    await db.execute(
        "INSERT INTO html_cache (key, html, via, fetched_at) VALUES (?,?,?,now()) "
        "ON CONFLICT(key) DO UPDATE SET html=excluded.html, via=excluded.via, "
        "fetched_at=excluded.fetched_at",
        (_cache_key(asin, marketplace), html[:MAX_CACHE_BYTES], via),
    )


async def cache_prune(days: int = 7) -> None:
    await db.execute(
        "DELETE FROM html_cache WHERE fetched_at < now() - make_interval(days => ?)", (days,)
    )


# ------------------------------------------------------------------ tầng fetch
async def fetch_direct(url: str, client: httpx.AsyncClient) -> str | None:
    """Trả HTML nếu qua được, None nếu bị chặn. Không raise cho trường hợp bị chặn."""
    await _throttle()
    try:
        resp = await client.get(url, headers=_headers(), timeout=25)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return None if parser.looks_blocked(resp.text) else resp.text


async def fetch_scrapedo(url: str, client: httpx.AsyncClient) -> str:
    if not settings.scrapedo_token:
        raise FetchError("no_token", "Chưa cấu hình SCRAPEDO_TOKEN")
    try:
        # KHÔNG truyền super=true — chế độ thường đã đủ cho Amazon, rẻ hơn 5-25 lần.
        resp = await client.get(
            SCRAPEDO_ENDPOINT,
            params={"token": settings.scrapedo_token, "url": url},
            timeout=90,
        )
    except httpx.HTTPError as exc:
        raise FetchError("network", f"Lỗi mạng khi gọi Scrape.do: {exc}") from exc

    # Scrape.do trả credit còn lại trong header — chính xác hơn tự đếm.
    # Mọi request đều tính phí, kể cả khi Amazon trả 404 -> đồng bộ trước khi raise.
    remaining = resp.headers.get("scrape.do-remaining-credits")
    if remaining and remaining.isdigit():
        await quota.set_remaining("scrapedo", int(remaining))
    else:
        await quota.bump("scrapedo")

    if resp.status_code == 401:
        raise FetchError("bad_token", "Scrape.do trả 401 — token sai")
    if resp.status_code in (402, 429):
        raise FetchError("quota", f"Scrape.do trả {resp.status_code} — hết quota hoặc rate limit")
    if resp.status_code == 404:
        # 404 truyền thẳng từ Amazon: ASIN không tồn tại hoặc sản phẩm đã bị gỡ.
        # KHÔNG phải lỗi provider, và retry cũng vô ích.
        raise FetchError(
            "not_found", "Sản phẩm không tồn tại trên Amazon (404) — kiểm tra lại ASIN"
        )
    if resp.status_code != 200:
        raise FetchError("provider", f"Scrape.do trả HTTP {resp.status_code}")
    if parser.looks_blocked(resp.text):
        raise FetchError("blocked", "Scrape.do lấy được nhưng Amazon vẫn trả trang chặn")
    return resp.text


async def fetch_html(
    asin: str, marketplace: str, domain: str, client: httpx.AsyncClient, *, force: bool = False
) -> tuple[str, str]:
    """Trả (html, via). via: cache | direct | scrapedo"""
    if not force and (hit := await cache_get(asin, marketplace)):
        return hit[0], "cache"

    url = urls.product_url(asin, domain)

    if settings.fetch_direct_first and (html := await fetch_direct(url, client)):
        await cache_put(asin, marketplace, html, "direct")
        return html, "direct"

    html = await fetch_scrapedo(url, client)
    await cache_put(asin, marketplace, html, "scrapedo")
    return html, "scrapedo"


async def fetch_and_parse(
    raw_url: str, client: httpx.AsyncClient, *, force: bool = False
) -> tuple[ProductData, str]:
    """Link thô -> (ProductData, via). Entry point cho job runner."""
    target = raw_url.strip()
    absolute = target if "://" in target else "https://" + target
    if urlparse(absolute).netloc.lower() in urls.SHORTENERS:
        target = await urls.resolve_shortlink(absolute, client)

    p = urls.parse(target)
    html, via = await fetch_html(p.asin, p.marketplace, p.domain, client, force=force)
    data = parser.parse_product(
        html, p.asin, p.marketplace, max_images=settings.max_images_per_product
    )
    return data, via
