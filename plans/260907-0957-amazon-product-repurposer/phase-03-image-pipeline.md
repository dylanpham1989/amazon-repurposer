# Phase 3 — Pipeline ảnh & Sticker

**Priority:** P1 · **Effort:** 10h · **Status:** ✅ Done (2026-09-07)
**Context:** [plan.md](./plan.md) · [Phase 2](./phase-02-fetch-and-parse.md)

## Overview

URL ảnh → nâng độ phân giải cao nhất → tải → dán sticker → WebP → R2 → CDN URL.
Đây là yêu cầu #1 của user và là phase có giá trị nhất.

## Key Insights

- **Nâng hi-res là ~10 dòng code nhưng quyết định cả app có dùng được không.** Ảnh trong
  HTML thường là thumbnail. Bỏ modifier block trong URL cho ảnh gốc 1500–3000px.
- **Ảnh Amazon không đồng nhất**: PNG trong suốt, CMYK JPEG, ảnh xoay theo EXIF.
  Không normalize trước khi composite → lỗi hoặc màu sai.
- **Pillow chặn event loop.** Phải `asyncio.to_thread`. Không cần ProcessPool ở quy mô này
  — thread là đủ vì Pillow nhả GIL trong hầu hết thao tác nặng.
- **Scale sticker theo % chiều rộng ảnh, KHÔNG theo pixel.** Ảnh có kích thước khác nhau;
  pixel cứng làm sticker khi to khi nhỏ bất nhất.
- **R2 key theo hash nội dung** → chạy lại không upload trùng → tiết kiệm quota 1M Class A free.

## Requirements

- Nâng hi-res với fallback chain
- Tải song song có giới hạn, validate
- Normalize: RGB, EXIF rotate, nền trắng cho ảnh trong suốt, resize ≤2000px
- Dán sticker: 9 anchor, scale/offset theo %, opacity, xoay, điều kiện
- WebP q=85, upload R2, key deterministic
- 1 ảnh lỗi không làm hỏng cả sản phẩm

## Architecture

### Nâng hi-res

```python
MODIFIER_RE = re.compile(r"\._[A-Za-z0-9_,\-]+_\.")

def hires_candidates(url: str) -> list[str]:
    stripped = MODIFIER_RE.sub(".", url)
    out = []
    if stripped != url:
        out += [stripped,                            # ảnh gốc — tốt nhất
                MODIFIER_RE.sub("._SL1600_.", url),
                MODIFIER_RE.sub("._SL1500_.", url)]
    out.append(url)                                  # fallback cuối
    return list(dict.fromkeys(out))
```

Chọn: `HEAD` từng URL (timeout 5s), lấy cái đầu tiên `200` và `content-length` lớn hơn
URL gốc ×1.1. Server không trả `content-length` → tải luôn rồi so byte thật.

### Tải & validate

```python
sem = asyncio.Semaphore(4)                  # nhẹ nhàng với CDN Amazon
# httpx stream, abort khi vượt max_image_bytes
MAGIC = {b"\xff\xd8\xff": "jpeg", b"\x89PNG": "png", b"RIFF": "webp"}
Image.MAX_IMAGE_PIXELS = 50_000_000         # chặn decompression bomb
```

Validate: magic bytes (**không tin `Content-Type`**) → `Image.verify()` →
dimension ≥ 200×200 → bytes trong `[2KB, max_image_bytes]`.

**Allowlist host** (chống SSRF — URL đến từ HTML bên thứ 3):
`m.media-amazon.com`, `images-na.ssl-images-amazon.com`, `images-eu.ssl-images-amazon.com`,
`*.media-amazon.com`. Không khớp → bỏ qua ảnh đó.

### Normalize

```python
def normalize(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)              # xoay theo EXIF
    if img.mode == "P":     img = img.convert("RGBA")
    elif img.mode == "CMYK": img = img.convert("RGB")
    elif img.mode not in ("RGB","RGBA"): img = img.convert("RGB")
    if img.mode == "RGBA":                          # nền trắng cho ảnh trong suốt
        bg = Image.new("RGB", img.size, (255,255,255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    img.thumbnail((2000, 2000), Image.LANCZOS)
    return img                                       # EXIF mất khi tạo Image mới
```

### Sticker engine

```python
ANCHORS = {
    "top-left":(0.,0.),    "top-center":(.5,0.),    "top-right":(1.,0.),
    "middle-left":(0.,.5), "center":(.5,.5),        "middle-right":(1.,.5),
    "bottom-left":(0.,1.), "bottom-center":(.5,1.), "bottom-right":(1.,1.),
}

def resolve_offset(anchor: str, ox: float, oy: float) -> tuple[float,float]:
    """User nghĩ 'cách mép 3%'. Đảo dấu để đúng trực giác ở mọi góc."""
    if "right"  in anchor: ox = -ox
    if "bottom" in anchor: oy = -oy
    return ox, oy

def apply_layer(base: Image.Image, sticker: Image.Image, L: Layer) -> Image.Image:
    W, H = base.size
    tw = int(W * L.scale)                                    # scale theo % ảnh nền
    sticker = sticker.resize((tw, int(sticker.height * tw / sticker.width)), Image.LANCZOS)
    if L.rotation:
        sticker = sticker.rotate(L.rotation, resample=Image.BICUBIC, expand=True)
    if L.opacity < 1.0:
        a = sticker.getchannel("A").point(lambda p: int(p * L.opacity))
        sticker.putalpha(a)
    ax, ay = ANCHORS[L.anchor]
    ox, oy = resolve_offset(L.anchor, L.offset_x, L.offset_y)
    sw, sh = sticker.size
    x = int(ax*W - ax*sw + ox*W)
    y = int(ay*H - ay*sh + oy*H)
    x = max(-sw//2, min(x, W - sw//2))                       # clamp
    y = max(-sh//2, min(y, H - sh//2))
    base = base.convert("RGBA")
    base.alpha_composite(sticker, (x, y))
    return base.convert("RGB")
```

### Layer schema (JSON trong `templates.layers`)

```python
class Layer(BaseModel):
    asset_id: str
    anchor: Literal[...9 giá trị...]
    scale: float = Field(0.15, ge=0.02, le=0.60)
    offset_x: float = Field(0.03, ge=0., le=0.30)     # "cách mép X%"
    offset_y: float = Field(0.03, ge=0., le=0.30)
    opacity: float = Field(1.0, ge=0.1, le=1.0)
    rotation: float = Field(0.0, ge=-180, le=180)
    apply_to: Literal["all","first_only"] = "all"
    when: Literal["always","is_best_seller","has_free_delivery"] = "always"
```

Điều kiện **cố tình chỉ có 3 giá trị** — đủ cho yêu cầu thật, không cần engine điều kiện tổng quát.

```python
def applies(L: Layer, p: ProductData, idx: int) -> bool:
    if L.apply_to == "first_only" and idx != 0: return False
    if L.when == "always": return True
    return bool(getattr(p, L.when, False))     # thiếu dữ liệu -> KHÔNG dán
```

**Nguyên tắc: thiếu dữ liệu → không dán.** Dán "Best Seller" sai sự thật nguy hiểm hơn thiếu badge.

### Template mặc định (seed lúc chạy lần đầu)

Khớp đúng ví dụ user đưa ra:
```json
[
 {"asset":"best-seller",  "anchor":"top-right",    "scale":0.20,
  "apply_to":"first_only","when":"is_best_seller"},
 {"asset":"free-delivery","anchor":"bottom-right", "scale":0.22,
  "apply_to":"all",       "when":"has_free_delivery"}
]
```

### Encode & upload

```python
buf = io.BytesIO(); img.save(buf, format="WEBP", quality=85, method=4)
key = f"p/{marketplace}/{asin}/{tpl_hash[:8]}/{pos:02d}-{sha256(data)[:12]}.webp"
# head_object trước -> đã có thì bỏ qua upload (tiết kiệm quota Class A)
# CacheControl: public, max-age=31536000, immutable   (key có hash -> an toàn)
```

`tpl_hash` = sha256 của template JSON đã canonicalize (`sort_keys=True, separators=(',',':')`)
→ đổi template sinh key mới, ảnh cũ vẫn còn.

### Upload sticker asset

- **Chỉ PNG có alpha.** JPEG → từ chối, message rõ ("sticker cần nền trong suốt, dùng PNG").
- ≤2MB, ≤1500×1500, magic bytes, `Image.verify()`
- **Re-encode** sau validate (`open()` → `save()`) → loại bỏ payload nhúng trong chunk PNG
- Key: `stickers/{sha256[:16]}.png`

## Related Code Files

**Create:**
- `app/services/images.py`        — hires, download, normalize, encode, pipeline
- `app/services/sticker.py`       — `ANCHORS`, `resolve_offset`, `apply_layer`, `applies`
- `app/services/templates.py`     — CRUD template + asset upload + seed
- `app/assets/best-seller.png`, `app/assets/free-delivery.png`, `app/assets/sample.png`
- `tests/fixtures/images/`        — jpeg thường, png alpha, cmyk, exif-rotated, ảnh khổng lồ
- `tests/test_hires.py`, `tests/test_sticker.py`, `tests/test_image_pipeline.py`

**Modify:** `app/storage.py` (đã có `put()` từ Phase 1)

## Implementation Steps

1. **`hires_candidates()` + HEAD probing.** Test bằng URL Amazon thật.
2. **Downloader** — streaming, size cap, magic bytes, `verify()`, bomb guard, host allowlist.
3. **`normalize()`** — test đủ 5 loại fixture. Assert output luôn RGB, cạnh dài ≤2000.
4. **`sticker.py`** — `resolve_offset` + `apply_layer` + `applies`.
   Test bằng pixel: anchor `top-right`, scale 0.2, offset 0.03 trên ảnh trắng 1000×1000
   → pixel tại `(1000-30-100, 30+50)` phải khác trắng.
5. **Encoder WebP** + so sánh size với JPEG.
6. **Template hash canonicalize** — test: cùng template khác thứ tự key → cùng hash.
7. **Key builder + `put()` idempotent** (`head_object` trước).
8. **Pipeline ghép 6 bước**, lỗi từng ảnh cô lập (`asyncio.gather(..., return_exceptions=True)`).
9. **Asset upload** với re-encode.
10. **Seed** 2 sticker mẫu + template mặc định lúc chạy lần đầu.
11. **CLI**: `uv run python -m app.cli image <url> --template <id>` → lưu ra file local để xem mắt.

## Todo List

- [x] `hires_candidates()` + HEAD probing + fallback 4 bước
- [x] Downloader streaming + size cap + timeout + `Semaphore(4)`
- [x] Magic bytes validation + `Image.verify()`
- [x] `Image.MAX_IMAGE_PIXELS = 50_000_000`
- [x] Host allowlist (chống SSRF)
- [x] `normalize()`: EXIF rotate, mode convert, nền trắng, resize
- [x] `ANCHORS` 9 vị trí + `resolve_offset()` đảo dấu theo góc
- [x] `apply_layer()`: scale %, offset %, opacity, rotation, clamp
- [x] `applies()`: `apply_to` + `when`, thiếu dữ liệu → không dán
- [x] WebP encoder q=85
- [x] Canonicalize template JSON → hash
- [x] R2 key deterministic + `head_object` trước upload
- [x] `Cache-Control: immutable`
- [x] Pipeline + cô lập lỗi từng ảnh
- [x] Chạy Pillow trong `asyncio.to_thread`
- [x] Asset upload: chỉ PNG có alpha, re-encode sau validate
- [x] Seed 2 sticker + template mặc định
- [x] CLI `image` xuất ra file local
- [x] Test: 5 fixture ảnh, sticker 4 góc bằng pixel, idempotency

## Success Criteria

- [x] ASIN thật: tải đủ ảnh, mỗi ảnh cạnh dài **≥1000px** (chứng minh hi-res hoạt động)
- [x] Mở ảnh output bằng mắt: sticker đúng góc, kích thước hợp lý, không che sản phẩm
- [x] Ảnh CMYK / PNG-alpha / EXIF-rotated → RGB đúng hướng, nền trắng
- [x] Chạy pipeline 2 lần cùng input → R2 chỉ 1 object, lần 2 **không** gọi `put_object`
- [x] Đổi template → key mới, ảnh cũ vẫn còn
- [x] Sản phẩm `is_best_seller=False` → layer Best Seller **không** dán (assert pixel)
- [x] `apply_to="first_only"` → chỉ ảnh index 0 có sticker
- [x] 1 ảnh 404 → các ảnh còn lại vẫn xong, sản phẩm `done`, ảnh đó `failed`
- [x] Ảnh 10000×10000 → `DecompressionBombError` bắt được, app không crash
- [x] Upload JPEG làm sticker → từ chối với message rõ ràng
- [x] CDN URL mở được trên trình duyệt

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| Pillow chặn event loop → app đứng | Cao nếu quên | Cao | `asyncio.to_thread` bắt buộc cho mọi thao tác Pillow |
| Hi-res trả 403/404 → mất ảnh đẹp | Trung bình | Trung bình | Fallback chain 4 bước, cuối cùng luôn dùng URL gốc |
| Sticker che mất chi tiết sản phẩm | Cao | Trung bình | Scale mặc định nhỏ (0.15–0.22); preview ở Phase 5 trước khi chạy batch |
| OOM khi ảnh lớn | Thấp | Trung bình | `MAX_IMAGE_PIXELS`; resize sớm trước composite |
| Đầy 10GB free tier R2 | Thấp (>10 tháng ở 30 SP/ngày) | Trung bình | Nút xoá batch cũ (xoá cả object R2); hiển thị dung lượng đã dùng |
| Sticker upload chứa payload độc | Thấp | Trung bình | Re-encode sau validate; serve `Content-Type` cứng |

## Security Considerations

- **SSRF**: URL ảnh đến từ HTML Amazon (không tin cậy) → allowlist host bắt buộc, chặn IP private.
- **Decompression bomb**: `MAX_IMAGE_PIXELS = 50_000_000`, không bỏ.
- **Không tin `Content-Type`** — chỉ magic bytes.
- **Pillow CVE**: pin version, chạy `pip-audit` định kỳ. Pillow có lịch sử CVE parse ảnh.
- Strip EXIF (có thể chứa GPS) — `normalize()` đã làm khi tạo Image mới.
- Asset user upload: re-encode để loại payload nhúng.
- R2 không bật public listing; key có hash nên khó đoán.

## Next Steps

→ [Phase 4 — AI Rewrite bằng Gemini](./phase-04-ai-rewrite.md)
