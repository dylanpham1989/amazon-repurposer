"""Pipeline ảnh: nâng hi-res -> tải -> normalize -> dán sticker -> WebP -> R2."""

from __future__ import annotations

import asyncio
import io
import re
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps

from app import storage
from app.config import settings
from app.models import ProductData
from app.services.sticker import Layer, applies, apply_layer, template_hash

# Chặn decompression bomb. Bắt buộc — ảnh nén nhỏ có thể giải nén thành hàng GB.
Image.MAX_IMAGE_PIXELS = 50_000_000

WEBP_QUALITY = 85
MIN_DIM = 200
MIN_BYTES = 2_048

# URL ảnh đến từ HTML bên thứ 3 -> allowlist chống SSRF
ALLOWED_IMAGE_HOSTS = (
    "m.media-amazon.com",
    "images-na.ssl-images-amazon.com",
    "images-eu.ssl-images-amazon.com",
    "images-fe.ssl-images-amazon.com",
)

MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
}

MODIFIER_RE = re.compile(r"\._[A-Za-z0-9_,\-]+_\.")


class ImageError(Exception):
    pass


# ------------------------------------------------------------------ hi-res
def hires_candidates(url: str) -> list[str]:
    """Ảnh trong HTML thường là thumbnail. Bỏ modifier block -> ảnh gốc 1500-3000px."""
    stripped = MODIFIER_RE.sub(".", url)
    out: list[str] = []
    if stripped != url:
        out += [
            stripped,                                   # ảnh gốc — tốt nhất
            MODIFIER_RE.sub("._SL1600_.", url),
            MODIFIER_RE.sub("._SL1500_.", url),
        ]
    out.append(url)                                     # fallback cuối
    return list(dict.fromkeys(out))


def host_allowed(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host in ALLOWED_IMAGE_HOSTS or host.endswith(".media-amazon.com")


async def pick_hires(url: str, client: httpx.AsyncClient) -> str:
    """HEAD từng candidate, lấy cái đầu tiên 200 và nặng hơn bản gốc rõ rệt."""
    candidates = hires_candidates(url)
    base_len = 0
    best = url
    for i, cand in enumerate(candidates):
        if not host_allowed(cand):
            continue
        try:
            r = await client.head(cand, timeout=6, follow_redirects=True)
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        size = int(r.headers.get("content-length") or 0)
        if i == len(candidates) - 1:            # bản gốc trong HTML
            base_len = size
            if best == url:
                best = cand
            continue
        if size and size > base_len * 1.1:
            return cand
        if not size:                            # server không trả content-length
            return cand
    return best


# ------------------------------------------------------------------ tải
def _sniff(data: bytes) -> str | None:
    for magic, fmt in MAGIC.items():
        if data.startswith(magic):
            return fmt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def download(url: str, client: httpx.AsyncClient) -> bytes:
    if not host_allowed(url):
        raise ImageError(f"host không nằm trong allowlist: {urlparse(url).netloc}")
    buf = bytearray()
    async with client.stream("GET", url, timeout=30, follow_redirects=True) as resp:
        if resp.status_code != 200:
            raise ImageError(f"HTTP {resp.status_code}")
        async for chunk in resp.aiter_bytes(65536):
            buf.extend(chunk)
            if len(buf) > settings.max_image_bytes:
                raise ImageError(f"ảnh vượt {settings.max_image_bytes} bytes")
    data = bytes(buf)
    if len(data) < MIN_BYTES:
        raise ImageError(f"ảnh quá nhỏ ({len(data)} bytes)")
    # KHÔNG tin Content-Type — chỉ magic bytes
    if _sniff(data) is None:
        raise ImageError("không nhận diện được định dạng ảnh")
    return data


# ------------------------------------------------------------------ xử lý
def normalize(raw: bytes) -> Image.Image:
    """Ảnh Amazon không đồng nhất: CMYK, PNG alpha, EXIF xoay. Chuẩn hoá hết."""
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
    except Image.DecompressionBombError as exc:
        raise ImageError("ảnh quá lớn (nghi decompression bomb)") from exc
    except Exception as exc:
        raise ImageError(f"file ảnh hỏng: {type(exc).__name__}") from exc

    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)

    if img.mode == "P":
        img = img.convert("RGBA")           # palette có thể mang transparency
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")            # CMYK, L, LA, I;16...

    if img.mode == "RGBA":                      # nền trắng cho ảnh trong suốt
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg

    if min(img.size) < MIN_DIM:
        raise ImageError(f"ảnh quá nhỏ {img.size}")

    img.thumbnail((settings.image_max_edge, settings.image_max_edge), Image.LANCZOS)
    return img                                   # EXIF mất khi tạo Image mới


def compose(raw: bytes, stickers: list[tuple[Layer, bytes]]) -> tuple[bytes, int, int]:
    """Blocking — gọi qua asyncio.to_thread. Trả (webp_bytes, w, h).
    Đóng Image tường minh: Pillow giữ buffer decode khá lâu nếu để GC tự lo."""
    img = normalize(raw)
    try:
        for layer, sticker_bytes in stickers:
            with Image.open(io.BytesIO(sticker_bytes)) as st:
                sticker = st.convert("RGBA")
            prev = img
            img = apply_layer(img, sticker, layer)
            sticker.close()
            if prev is not img:
                prev.close()
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=WEBP_QUALITY, method=settings.webp_method)
        return out.getvalue(), img.width, img.height
    finally:
        img.close()


# ------------------------------------------------------------------ key R2
def r2_key(marketplace: str, asin: str, tpl_hash: str, position: int, data: bytes) -> str:
    """Key deterministic theo nội dung -> chạy lại không upload trùng."""
    return (
        f"p/{marketplace}/{asin}/{tpl_hash[:8]}/"
        f"{position:02d}-{storage.content_hash(data)[:12]}.webp"
    )


# ------------------------------------------------------------------ pipeline
class ImageResult:
    __slots__ = ("position", "source_url", "hires_url", "cdn_url", "r2_key",
                 "width", "height", "bytes", "uploaded", "error")

    def __init__(self, position: int, source_url: str):
        self.position = position
        self.source_url = source_url
        self.hires_url: str | None = None
        self.cdn_url: str | None = None
        self.r2_key: str | None = None
        self.width = self.height = self.bytes = 0
        self.uploaded = False
        self.error: str | None = None


async def process_one(
    position: int,
    url: str,
    product: ProductData,
    layers: list[Layer],
    assets: dict[str, bytes],
    tpl_hash: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> ImageResult:
    res = ImageResult(position, url)
    try:
        # Semaphore ôm TRỌN vòng đời của ảnh (tải + xử lý + upload), không chỉ bước tải.
        # Nếu thả sớm, N task đã tải xong vẫn giữ raw bytes (tới 12MB mỗi cái) trong lúc
        # chờ threadpool -> đo được đỉnh 486MB/512MB, OOM trên Render free.
        async with sem:
            res.hires_url = await pick_hires(url, client)
            raw = await download(res.hires_url, client)

            active = [
                (layer, assets[layer.asset_id])
                for layer in layers
                if applies(layer, product, position) and layer.asset_id in assets
            ]
            # Pillow chặn event loop -> bắt buộc chạy trong thread
            data, w, h = await asyncio.to_thread(compose, raw, active)
            del raw                        # thả sớm, ảnh gốc có thể tới 12MB

            key = r2_key(product.marketplace, product.asin, tpl_hash, position, data)
            cdn, uploaded = await storage.put(key, data, "image/webp")
            res.width, res.height, res.bytes = w, h, len(data)
            del data
        res.r2_key, res.cdn_url, res.uploaded = key, cdn, uploaded
    except (ImageError, httpx.HTTPError) as exc:
        res.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - 1 ảnh lỗi không được kéo cả sản phẩm
        res.error = f"{type(exc).__name__}: {exc}"
    return res


async def process_product(
    product: ProductData,
    layers: list[Layer],
    assets: dict[str, bytes],
    client: httpx.AsyncClient,
    *,
    concurrency: int | None = None,
) -> list[ImageResult]:
    tpl_hash = template_hash(layers)
    sem = asyncio.Semaphore(concurrency or settings.image_concurrency)
    tasks = [
        process_one(i, url, product, layers, assets, tpl_hash, client, sem)
        for i, url in enumerate(product.images)
    ]
    return list(await asyncio.gather(*tasks))
