"""Quản lý template sticker + asset. Template là dữ liệu trong DB, không phải hằng số."""

from __future__ import annotations

import io
import json
import pathlib

from PIL import Image, ImageDraw

from app import db, storage
from app.services.sticker import Layer, applies, apply_layer

ASSET_DIR = pathlib.Path("data/assets")
MAX_ASSET_BYTES = 2_000_000
MAX_ASSET_DIM = 1500
SAMPLE_SIZE = (1000, 1000)


class AssetError(ValueError):
    pass


_asset_cache: dict[str, bytes] = {}


def asset_path(asset_id: str) -> pathlib.Path:
    return ASSET_DIR / f"{asset_id}.png"


# ------------------------------------------------------------------ asset
def validate_and_clean(raw: bytes) -> tuple[bytes, int, int]:
    """Chỉ nhận PNG có alpha. Re-encode để loại payload nhúng trong chunk PNG."""
    if len(raw) > MAX_ASSET_BYTES:
        raise AssetError(f"File quá lớn ({len(raw) // 1024} KB) — tối đa 2 MB")
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssetError("Chỉ nhận file PNG. Sticker cần nền trong suốt nên JPEG không dùng được.")
    try:
        Image.open(io.BytesIO(raw)).verify()
        img = Image.open(io.BytesIO(raw))
    except Exception as exc:
        raise AssetError(f"File PNG hỏng: {type(exc).__name__}") from exc

    if img.mode not in ("RGBA", "LA", "PA"):
        raise AssetError(
            "PNG này không có kênh trong suốt. Sticker phải có nền trong suốt, "
            "nếu không sẽ dán một ô vuông đặc lên ảnh sản phẩm."
        )
    img = img.convert("RGBA")
    if max(img.size) > MAX_ASSET_DIM:
        img.thumbnail((MAX_ASSET_DIM, MAX_ASSET_DIM), Image.LANCZOS)
    if img.getchannel("A").getextrema()[0] == 255:
        raise AssetError("Toàn bộ ảnh đục — không có vùng trong suốt nào.")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)      # re-encode = khử payload nhúng
    return buf.getvalue(), img.width, img.height


async def save_asset(name: str, raw: bytes, asset_id: str | None = None) -> str:
    clean, w, h = validate_and_clean(raw)
    aid = asset_id or db.new_id()[:12]

    _asset_cache[aid] = clean
    key = f"stickers/{storage.content_hash(clean)[:16]}.png"
    cdn, _ = await storage.put(key, clean, "image/png")

    await db.execute(
        "INSERT INTO assets (id,name,r2_key,cdn_url,width,height,data) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, r2_key=excluded.r2_key, "
        "cdn_url=excluded.cdn_url, width=excluded.width, height=excluded.height, "
        "data=excluded.data",
        (aid, name.strip() or aid, key, cdn, w, h, clean),
    )
    return aid


async def list_assets() -> list[dict]:
    return [dict(r) for r in await db.fetch_all("SELECT * FROM assets ORDER BY name")]


async def load_asset_bytes(asset_id: str) -> bytes | None:
    """Đọc từ Postgres, không từ disk — filesystem của Render là ephemeral.
    Cache trong process để không query lại mỗi ảnh."""
    if asset_id in _asset_cache:
        return _asset_cache[asset_id]
    row = await db.fetch_one("SELECT data FROM assets WHERE id=?", (asset_id,))
    if row and row["data"]:
        _asset_cache[asset_id] = bytes(row["data"])
        return _asset_cache[asset_id]
    return None


async def delete_asset(asset_id: str) -> None:
    row = await db.fetch_one("SELECT r2_key FROM assets WHERE id=?", (asset_id,))
    if row:
        await storage.delete_many([row["r2_key"]])
    _asset_cache.pop(asset_id, None)
    await db.execute("DELETE FROM assets WHERE id=?", (asset_id,))


# ------------------------------------------------------------------ template
async def list_templates() -> list[dict]:
    rows = await db.fetch_all("SELECT * FROM templates ORDER BY is_default DESC, created_at")
    out = []
    for r in rows:
        d = dict(r)
        d["layers"] = json.loads(r["layers"] or "[]")
        out.append(d)
    return out


async def get_template(tid: str) -> dict | None:
    row = await db.fetch_one("SELECT * FROM templates WHERE id=?", (tid,))
    if not row:
        return None
    d = dict(row)
    d["layers"] = [Layer(**x) for x in json.loads(row["layers"] or "[]")]
    return d


async def save_template(tid: str, name: str, layers: list[Layer], is_default: bool) -> None:
    payload = json.dumps([x.model_dump() for x in layers], ensure_ascii=False)
    if is_default:
        await db.execute("UPDATE templates SET is_default=0")
    await db.execute(
        "INSERT INTO templates (id,name,is_default,layers) VALUES (?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, is_default=excluded.is_default, "
        "layers=excluded.layers",
        (tid, name.strip() or "Template", int(is_default), payload),
    )


async def delete_template(tid: str) -> None:
    await db.execute("DELETE FROM templates WHERE id=? AND is_default=0", (tid,))


# ------------------------------------------------------------------ preview
def sample_image() -> Image.Image:
    """Ảnh mẫu sinh tại chỗ — không cần ship file binary."""
    img = Image.new("RGB", SAMPLE_SIZE, (255, 255, 255))
    d = ImageDraw.Draw(img)
    w, h = SAMPLE_SIZE
    d.rounded_rectangle([w * 0.22, h * 0.26, w * 0.78, h * 0.74], radius=36,
                        fill=(226, 228, 233), outline=(203, 206, 214), width=3)
    d.ellipse([w * 0.38, h * 0.40, w * 0.62, h * 0.60], fill=(203, 206, 214))
    d.text((w * 0.5, h * 0.82), "ẢNH SẢN PHẨM MẪU", fill=(150, 154, 163), anchor="mm")
    return img


def render_preview(layers: list[Layer], assets: dict[str, bytes], *, best_seller: bool = True,
                   free_delivery: bool = True) -> bytes:
    from app.models import ProductData

    fake = ProductData(asin="SAMPLE", marketplace="US",
                       is_best_seller=best_seller, has_free_delivery=free_delivery)
    img = sample_image()
    for layer in layers:
        if not applies(layer, fake, 0) or layer.asset_id not in assets:
            continue
        sticker = Image.open(io.BytesIO(assets[layer.asset_id])).convert("RGBA")
        img = apply_layer(img, sticker, layer)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=88)
    return buf.getvalue()


async def seed_default() -> None:
    """Chạy lúc startup: đảm bảo có ít nhất 1 template + asset."""
    legacy = pathlib.Path("img/best-seller.png")
    has_seed = await db.fetch_one("SELECT 1 FROM assets WHERE id='best-seller'")
    if legacy.exists() and not has_seed:
        await save_asset("Best Seller", legacy.read_bytes(), asset_id="best-seller")

    has_tpl = await db.fetch_one("SELECT 1 FROM templates LIMIT 1")
    has_asset = await db.fetch_one("SELECT 1 FROM assets WHERE id='best-seller'")
    if not has_tpl and has_asset:
        await save_template(
            "default", "Best Seller góc trên phải",
            [Layer(asset_id="best-seller", anchor="top-right", scale=0.16,
                   offset_x=0.03, offset_y=0.03, apply_to="all", when="always")],
            True,
        )
