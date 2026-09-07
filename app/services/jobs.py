"""Job chạy nền: asyncio.Task + cột status trong SQLite. Không Redis, không ARQ."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import httpx

from app import db
from app.config import settings
from app.models import ProductData
from app.services import fetcher, images, rewrite, urls
from app.services import templates as templates_svc
from app.services.sticker import Layer

_running: dict[str, asyncio.Task] = {}


async def load_template() -> tuple[list[Layer], dict[str, bytes]]:
    row = await db.fetch_one(
        "SELECT layers FROM templates WHERE is_default=1 ORDER BY created_at LIMIT 1"
    )
    if not row:
        return [], {}
    layers = [Layer(**x) for x in json.loads(row["layers"])]

    assets: dict[str, bytes] = {}
    for layer in layers:
        if (raw := await templates_svc.load_asset_bytes(layer.asset_id)) is not None:
            assets[layer.asset_id] = raw
    return layers, assets


async def create_batch(raw_urls: list[str]) -> tuple[str, list[str]]:
    """Validate ĐỒNG BỘ trước khi tạo gì — báo lỗi ngay thay vì enqueue rồi mới fail."""
    seen: set[tuple[str, str]] = set()
    items: list[tuple[str, str, str]] = []
    errors: list[str] = []

    for raw in raw_urls:
        raw = raw.strip()
        if not raw:
            continue
        try:
            p = urls.parse(raw)
        except urls.InvalidUrlError as exc:
            errors.append(f"{raw[:60]} — {exc}")
            continue
        key = (p.asin, p.marketplace)
        if key in seen:
            continue
        seen.add(key)
        items.append((raw, p.asin, p.marketplace))

    if errors:
        return "", errors
    if not items:
        return "", ["Không có link hợp lệ nào"]
    if len(items) > settings.max_links_per_batch:
        return "", [f"Tối đa {settings.max_links_per_batch} link mỗi lần"]

    batch_id = db.new_id()
    stmts: list[tuple[str, tuple]] = [
        ("INSERT INTO batches (id, status, total) VALUES (?, 'pending', ?)",
         (batch_id, len(items)))
    ]
    stmts += [
        ("INSERT INTO products (id, batch_id, source_url, asin, marketplace) VALUES (?,?,?,?,?)",
         (db.new_id(), batch_id, raw, asin, marketplace))
        for raw, asin, marketplace in items
    ]
    # Một transaction, commit xong mới chạy task — không thì worker đọc chưa thấy row.
    await db.execute_many(stmts)
    return batch_id, []


async def _set(product_id: str, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    await db.execute(f"UPDATE products SET {cols} WHERE id=?", (*fields.values(), product_id))


async def _process_product(row, layers, assets, client: httpx.AsyncClient) -> bool:
    pid = row["id"]
    try:
        await _set(pid, status="running", stage="fetch", error=None)
        data, via = await fetcher.fetch_and_parse(row["source_url"], client)
        await _set(
            pid,
            fetched_via=via,
            orig_title=data.title,
            orig_desc=data.description,
            bullets=json.dumps(data.bullets, ensure_ascii=False),
            brand=data.brand,
            price=data.price,
            desc_source=data.desc_source,
            is_best_seller=int(data.is_best_seller),
            has_free_delivery=int(data.has_free_delivery),
        )

        await _set(pid, stage="images")
        await db.execute("DELETE FROM images WHERE product_id=?", (pid,))
        results = await images.process_product(data, layers, assets, client)
        await db.execute_many([
            ("INSERT INTO images (id, product_id, position, source_url, r2_key, cdn_url, "
             "width, height, bytes, status, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             (db.new_id(), pid, r.position, r.source_url, r.r2_key, r.cdn_url,
              r.width, r.height, r.bytes, "failed" if r.error else "done", r.error))
            for r in results
        ])

        await _set(pid, stage="rewrite")
        out = await rewrite.rewrite(data, client)
        await _set(
            pid,
            new_title=out.title,
            new_desc=out.description,
            rewrite_status=out.status,
            rewrite_flags=json.dumps(out.flags, ensure_ascii=False),
        )

        # Ảnh hoặc rewrite lỗi vẫn tính là done — dữ liệu lấy được vẫn dùng được.
        await _set(pid, status="done", stage=None)
        return True
    except Exception as exc:  # noqa: BLE001
        await _set(pid, status="failed", stage=None, error=f"{type(exc).__name__}: {exc}")
        return False


async def run_batch(batch_id: str) -> None:
    """Tuần tự, không song song: fetch song song = bị Amazon chặn IP."""
    try:
        await db.execute("UPDATE batches SET status='running' WHERE id=?", (batch_id,))
        layers, assets = await load_template()
        rows = await db.fetch_all(
            "SELECT * FROM products WHERE batch_id=? AND status IN ('pending','failed') "
            "ORDER BY seq",
            (batch_id,),
        )
        async with httpx.AsyncClient(
            http2=True, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=8.0)
        ) as client:
            for row in rows:
                cur = await db.fetch_one("SELECT status FROM batches WHERE id=?", (batch_id,))
                if cur and cur["status"] == "cancelled":
                    break
                ok = await _process_product(row, layers, assets, client)
                await db.execute(
                    "UPDATE batches SET done = done + ?, failed = failed + ? WHERE id=?",
                    (1 if ok else 0, 0 if ok else 1, batch_id),
                )
                await asyncio.sleep(settings.request_delay_seconds)

        b = await db.fetch_one("SELECT done, failed, total, status FROM batches WHERE id=?",
                               (batch_id,))
        if b and b["status"] != "cancelled":
            status = "done" if b["failed"] == 0 else ("failed" if b["done"] == 0 else "partial")
            await db.execute("UPDATE batches SET status=? WHERE id=?", (status, batch_id))
    finally:
        _running.pop(batch_id, None)


def start(batch_id: str) -> None:
    if batch_id in _running:
        return
    _running[batch_id] = asyncio.create_task(run_batch(batch_id))


def is_running(batch_id: str) -> bool:
    return batch_id in _running


def _row(rec) -> dict:
    """asyncpg trả datetime thật (SQLite trả string) -> chuẩn hoá để JSON hoá được
    và hiển thị gọn trên UI."""
    d = dict(rec)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.strftime("%Y-%m-%d %H:%M")
        elif isinstance(v, memoryview | bytes):
            d[k] = None                       # cột BYTEA không đưa ra API
    return d


async def batch_view(batch_id: str) -> dict | None:
    b = await db.fetch_one("SELECT * FROM batches WHERE id=?", (batch_id,))
    if not b:
        return None
    prods = await db.fetch_all(
        "SELECT * FROM products WHERE batch_id=? ORDER BY seq", (batch_id,)
    )
    out = []
    for p in prods:
        imgs = await db.fetch_all(
            "SELECT * FROM images WHERE product_id=? ORDER BY position", (p["id"],)
        )
        d = _row(p)
        d["images"] = [_row(i) for i in imgs]
        d["cdn_urls"] = [i["cdn_url"] for i in imgs if i["cdn_url"]]
        d["flags"] = json.loads(p["rewrite_flags"] or "[]")
        out.append(d)
    return {"batch": _row(b), "products": out, "active": b["status"] in ("pending", "running")}


def product_data_from_row(row: dict) -> ProductData:
    return ProductData(
        asin=row["asin"] or "",
        marketplace=row["marketplace"] or "US",
        title=row["orig_title"] or "",
        description=row["orig_desc"] or "",
        desc_source=row["desc_source"] or "none",
        bullets=json.loads(row["bullets"] or "[]"),
        brand=row["brand"],
        is_best_seller=bool(row["is_best_seller"]),
        has_free_delivery=bool(row["has_free_delivery"]),
    )
