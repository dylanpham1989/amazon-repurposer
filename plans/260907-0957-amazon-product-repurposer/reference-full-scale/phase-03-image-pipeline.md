# Phase 3 — Image Pipeline & R2 Storage

**Priority:** P1 · **Effort:** 16h · **Status:** Pending
**Context:** [plan.md](./plan.md) · [Phase 2](./phase-02-ingestion-scraper-adapter.md) · [Research 01 §2–3](./research/research-01-providers-and-costs.md)

## Overview

Phase nặng nhất. Từ danh sách URL ảnh → tải bản độ phân giải cao nhất → dán sticker theo template → encode WebP → upload R2 → trả CDN URL.

## Key Insights

- **Nâng độ phân giải là bước có giá trị cao nhất và dễ bị bỏ sót.** Ảnh provider trả về thường là thumbnail 500px. Bỏ modifier block trong URL cho ảnh gốc 1500–3000px. Nhưng **không phải lúc nào cũng work** → cần chain thử + fallback.
- **Ảnh Amazon không đồng nhất:** có PNG trong suốt, CMYK JPEG, ảnh xoay theo EXIF, ảnh có alpha channel. Không normalize trước khi composite → lỗi hoặc màu sai.
- **Pillow blocking CPU.** Chạy trong `asyncio` event loop sẽ chặn worker. Bắt buộc `run_in_executor` với `ProcessPoolExecutor` (không phải Thread — Pillow giữ GIL ở một số op).
- **Storage tích lũy tuyến tính.** 2000 SP/ngày → 756 GB sau 12 tháng. Lifecycle policy là bắt buộc, không phải nice-to-have.
- **Idempotency.** Job retry không được upload trùng. Key R2 phải deterministic từ nội dung.

## Requirements

### Functional
- Nâng URL lên độ phân giải cao nhất có thể, có fallback chain
- Tải song song, giới hạn concurrency, timeout riêng mỗi ảnh
- Validate: magic bytes (không tin `Content-Type`), kích thước tối đa, dimension tối thiểu
- Normalize: RGB(A), strip EXIF, auto-rotate, resize cạnh dài ≤ 2000px
- Composite sticker theo template (Phase 4 cung cấp template; Phase 3 làm engine)
- Encode WebP q=85 (chính) + JPEG q=88 (fallback, cấu hình được)
- Upload R2 với key deterministic, `Cache-Control` dài
- Trả CDN URL, ghi metadata vào `product_images`

### Non-functional
- Xử lý 1 sản phẩm 7 ảnh ≤ 15s (p95)
- Một ảnh fail **không** làm fail cả sản phẩm — chỉ đánh dấu ảnh đó
- Peak memory mỗi ảnh < 300MB (ảnh 3000×3000 RGBA ≈ 36MB raw, có headroom)

## Architecture

### Bước 1 — Nâng độ phân giải

```python
MODIFIER_RE = re.compile(r"\._[A-Za-z0-9_,\-]+_\.")

def hires_candidates(url: str) -> list[str]:
    """Trả list URL để thử theo thứ tự ưu tiên."""
    stripped = MODIFIER_RE.sub(".", url)
    out = []
    if stripped != url:
        out.append(stripped)                                    # ảnh gốc — tốt nhất
        out.append(MODIFIER_RE.sub("._SL1600_.", url))
        out.append(MODIFIER_RE.sub("._SL1500_.", url))
    out.append(url)                                             # fallback cuối
    # dedupe giữ thứ tự
    return list(dict.fromkeys(out))
```

Chọn candidate: `HEAD` từng URL (timeout 5s). Chấp nhận URL đầu tiên có `status==200` **và** `content-length` > `content-length` của URL gốc × 1.1. Nếu server không trả `content-length` → tải luôn và so byte thực tế.

Ghi cả `source_url` và `hires_url` vào DB để audit.

### Bước 2 — Download

```python
sem = asyncio.Semaphore(6)          # concurrency mỗi sản phẩm
# httpx.AsyncClient dùng chung (tạo ở worker startup), http2=True
# timeout: connect 5s, read 20s, total 30s
# headers: User-Agent trình duyệt thật + Referer amazon domain
# stream download, abort khi vượt max_image_bytes
```

Validate ngay sau tải:
- **Magic bytes** (không tin `Content-Type`): JPEG `FF D8 FF`, PNG `89 50 4E 47`, WebP `RIFF....WEBP`, GIF `GIF8`
- `bytes` trong khoảng `[2KB, max_image_bytes]`
- Mở bằng `Image.open()` + `img.verify()` để phát hiện file hỏng
- Dimension ≥ 200×200 (nhỏ hơn thường là icon/placeholder)
- **Decompression bomb guard**: `Image.MAX_IMAGE_PIXELS = 50_000_000`. Pillow raise `DecompressionBombError` — bắt và fail ảnh đó.

### Bước 3 — Normalize (chạy trong ProcessPool)

```python
def normalize_image(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)          # xoay theo EXIF
    if img.mode == "P":
        img = img.convert("RGBA")               # palette có thể có transparency
    elif img.mode == "CMYK":
        img = img.convert("RGB")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    # nền trắng cho ảnh trong suốt (Amazon thường nền trắng sẵn)
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    # resize cạnh dài về tối đa 2000px, giữ tỉ lệ
    img.thumbnail((2000, 2000), Image.LANCZOS)
    return img   # EXIF đã mất khi tạo Image mới → không cần strip thêm
```

### Bước 4 — Sticker Composite Engine

Đây là core của yêu cầu #1 của user. Engine nhận `Image` + `list[Layer]` (Phase 4 định nghĩa schema layer).

```python
ANCHORS = {
    "top-left":      (0.0, 0.0), "top-center":    (0.5, 0.0), "top-right":     (1.0, 0.0),
    "middle-left":   (0.0, 0.5), "center":        (0.5, 0.5), "middle-right":  (1.0, 0.5),
    "bottom-left":   (0.0, 1.0), "bottom-center": (0.5, 1.0), "bottom-right":  (1.0, 1.0),
}

def apply_layer(base: Image.Image, sticker: Image.Image, layer: Layer) -> Image.Image:
    W, H = base.size

    # 1. Scale sticker theo % chiều rộng ảnh nền (KHÔNG dùng pixel tuyệt đối —
    #    ảnh có kích thước khác nhau, pixel cứng làm sticker to/nhỏ bất nhất)
    target_w = int(W * layer.scale)                    # scale: 0.05 – 0.5
    ratio = target_w / sticker.width
    sticker = sticker.resize((target_w, int(sticker.height * ratio)), Image.LANCZOS)

    # 2. Xoay (nếu có), expand=True để không cắt góc
    if layer.rotation:
        sticker = sticker.rotate(layer.rotation, resample=Image.BICUBIC, expand=True)

    # 3. Opacity
    if layer.opacity < 1.0:
        alpha = sticker.getchannel("A").point(lambda p: int(p * layer.opacity))
        sticker.putalpha(alpha)

    # 4. Vị trí theo anchor + offset (offset cũng theo % để scale-invariant)
    ax, ay = ANCHORS[layer.anchor]
    sw, sh = sticker.size
    x = int(ax * W - ax * sw + layer.offset_x * W)
    y = int(ay * H - ay * sh + layer.offset_y * H)
    # clamp để sticker không rơi hoàn toàn ra ngoài
    x = max(-sw // 2, min(x, W - sw // 2))
    y = max(-sh // 2, min(y, H - sh // 2))

    base = base.convert("RGBA")
    base.alpha_composite(sticker, (x, y))
    return base.convert("RGB")
```

Ghi chú:
- Sticker asset **phải là PNG có alpha**. Validate lúc upload (Phase 4).
- Layer áp theo thứ tự trong list (index 0 dưới cùng).
- Layer có `apply_to`: `all` | `first_only` | `except_first`. Thực tế thường chỉ muốn badge trên ảnh chính.
- Layer có `condition` (Phase 4): ví dụ sticker "Best Seller" chỉ dán khi `product.is_best_seller == True`.

### Bước 5 — Encode & Upload

```python
# WebP là format chính: nhỏ hơn JPEG ~30% ở cùng chất lượng, mọi trình duyệt hiện đại đều hỗ trợ
buf = io.BytesIO()
img.save(buf, format="WEBP", quality=85, method=4)

# JPEG fallback — chỉ tạo nếu settings.emit_jpeg_fallback = True
# Cân nhắc: gấp đôi storage. Mặc định TẮT.
```

**R2 key deterministic (idempotency):**
```
products/{marketplace}/{asin}/{template_hash[:8]}/{position:02d}-{content_sha256[:12]}.webp
```
- `template_hash` = sha256 của template JSON đã canonicalize → đổi template sinh key mới, không ghi đè bản cũ
- `content_sha256` = hash của bytes ảnh **sau khi xử lý** → retry sinh đúng key cũ, upload lại vô hại
- Trước khi PUT: `head_object` — nếu đã tồn tại, bỏ qua upload, dùng luôn CDN URL. Tiết kiệm Class A ops.

Upload metadata:
```python
await s3.put_object(
    Bucket=settings.r2_bucket, Key=key, Body=data,
    ContentType="image/webp",
    CacheControl="public, max-age=31536000, immutable",   # key có hash → immutable an toàn
    Metadata={"asin": asin, "position": str(pos), "template": template_hash[:8]},
)
```

CDN URL: `f"{settings.r2_public_base_url}/{key}"`

### Bước 6 — Lifecycle Policy (BẮT BUỘC)

Cấu hình trên R2 bucket:
- Prefix `products/`, sau **90 ngày** → chuyển `InfrequentAccess` ($0.010/GB thay vì $0.015)
- Sau **180 ngày** → xoá

Bảo vệ dữ liệu cần giữ: batch được user đánh dấu `keep=true` hoặc đã export → job nền copy sang prefix `archive/` (không nằm trong lifecycle rule). Chạy trước khi lifecycle chạm tới.

**Không có bước này** storage tăng tuyến tính vĩnh viễn.

### Concurrency model

```
Worker (ARQ, max_jobs=8)
 └── process_product_images(product_id)          # 1 job / sản phẩm
      ├── asyncio.Semaphore(6) cho HTTP download
      └── ProcessPoolExecutor(max_workers=cpu_count) cho Pillow
```

`ProcessPoolExecutor` tạo **một lần** ở worker startup, không tạo per-job (fork tốn kém).
Truyền `bytes` qua process boundary, không truyền `Image` object (không pickle được hiệu quả).

## Related Code Files

**Create:**
- `apps/api/src/app/services/images/__init__.py`
- `apps/api/src/app/services/images/hires.py`         — nâng độ phân giải
- `apps/api/src/app/services/images/downloader.py`    — tải + validate
- `apps/api/src/app/services/images/processor.py`     — normalize + composite (chạy trong ProcessPool)
- `apps/api/src/app/services/images/compositor.py`    — sticker engine
- `apps/api/src/app/services/images/encoder.py`       — WebP/JPEG encode
- `apps/api/src/app/services/storage/r2.py`           — aioboto3 client, put/head/delete
- `apps/api/src/app/services/storage/keys.py`         — key builder + hashing
- `apps/api/src/app/services/images/pipeline.py`      — orchestrate 6 bước
- `apps/api/tests/test_hires.py`
- `apps/api/tests/test_compositor.py`
- `apps/api/tests/test_image_pipeline.py`
- `apps/api/tests/fixtures/images/`  (ảnh mẫu: JPEG thường, PNG alpha, CMYK, EXIF-rotated, ảnh khổng lồ)
- `scripts/setup_r2_lifecycle.py`                     — apply lifecycle rule

**Modify:**
- `apps/api/src/app/worker.py` — đăng ký ProcessPoolExecutor + httpx client ở startup

## Implementation Steps

1. **`hires.py`** — `hires_candidates()` + `pick_best_candidate()` với HEAD probing. Test bằng URL Amazon thật.
2. **`downloader.py`** — streaming download có giới hạn size, magic-byte validation, `Image.verify()`, bomb guard.
3. **`processor.py::normalize_image`** — test với đủ 5 loại ảnh fixture. Assert output luôn RGB, cạnh dài ≤2000.
4. **`compositor.py`** — sticker engine. Test bằng cách so sánh pixel tại vị trí kỳ vọng (ví dụ: sticker anchor `top-right` → pixel ở `(W-10, 10)` phải khác nền trắng).
5. **`encoder.py`** — WebP encode, đo size, so sánh với JPEG cùng chất lượng.
6. **`storage/keys.py`** — canonicalize template JSON (sort key, no whitespace) → sha256. Test: cùng template khác thứ tự key → cùng hash.
7. **`storage/r2.py`** — aioboto3 session dùng chung. `head_object` trước `put_object`. Test bằng MinIO trong docker-compose.
8. **`pipeline.py`** — ghép 6 bước, xử lý lỗi per-image (không fail cả product), cập nhật `product_images` từng bước.
9. **ProcessPoolExecutor lifecycle** trong `worker.py`.
10. **`scripts/setup_r2_lifecycle.py`** — chạy 1 lần, apply lifecycle rule qua `put_bucket_lifecycle_configuration`.
11. **CLI debug**: `python -m app.cli process-image <url> --template <id>` → lưu ra file local để xem mắt thường.

## Todo List

- [ ] `hires_candidates()` + HEAD probing + fallback chain
- [ ] Streaming downloader + size cap + timeout
- [ ] Magic-byte validation (JPEG/PNG/WebP/GIF)
- [ ] Decompression bomb guard (`MAX_IMAGE_PIXELS`)
- [ ] `normalize_image()`: EXIF rotate, mode convert, nền trắng cho alpha, resize
- [ ] Sticker compositor: 9 anchor, scale %, offset %, opacity, rotation, clamp
- [ ] `apply_to` filter (all / first_only / except_first)
- [ ] WebP encoder + JPEG fallback (tắt mặc định)
- [ ] Template hash canonicalization
- [ ] R2 key builder deterministic
- [ ] `r2.py`: put/head/delete với aioboto3, `head_object` trước upload
- [ ] `Cache-Control: immutable` + custom metadata
- [ ] `pipeline.py` orchestration + per-image error isolation
- [ ] ProcessPoolExecutor ở worker startup, teardown sạch
- [ ] MinIO trong docker-compose cho test local
- [ ] `scripts/setup_r2_lifecycle.py` + chạy trên bucket prod
- [ ] Cơ chế `archive/` cho batch `keep=true`
- [ ] CLI debug xuất ảnh ra local
- [ ] Test: 5 loại ảnh fixture, compositor 9 anchor, idempotency (chạy 2 lần → 1 upload)

## Success Criteria

- [ ] Với ASIN thật: tải đủ số ảnh, mỗi ảnh cạnh dài ≥ 1000px (chứng minh hi-res upgrade hoạt động)
- [ ] Ảnh output mở được, có sticker đúng vị trí — kiểm tra bằng mắt qua CLI debug
- [ ] Ảnh CMYK / PNG-alpha / EXIF-rotated đều ra RGB đúng hướng, nền trắng
- [ ] Chạy pipeline 2 lần cùng input → R2 chỉ có 1 object, lần 2 không gọi `put_object` (assert)
- [ ] Đổi template → sinh key mới, ảnh cũ vẫn còn
- [ ] 1 ảnh 404 giữa chừng → 6 ảnh còn lại vẫn xong, product status `completed`, ảnh lỗi status `failed`
- [ ] Sản phẩm 7 ảnh xử lý xong ≤ 15s (p95, đo local)
- [ ] Ảnh 10000×10000 → `DecompressionBombError` bắt được, không crash worker
- [ ] Lifecycle rule hiện đúng khi `get_bucket_lifecycle_configuration`
- [ ] CDN URL truy cập được từ trình duyệt, header `Cache-Control` đúng

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| **Storage phình không kiểm soát** | **Cao** nếu bỏ qua | **Cao** | Lifecycle policy Phase 3 (không hoãn sang Phase 8); dashboard theo dõi GB/tháng |
| Pillow chặn event loop → worker đứng | Cao nếu làm sai | Cao | `ProcessPoolExecutor` bắt buộc; test có assert event loop không bị block |
| Memory leak / OOM khi xử lý ảnh lớn | Trung bình | Cao | `MAX_IMAGE_PIXELS`; resize sớm trước composite; `max_tasks_per_child` cho ProcessPool để recycle worker |
| Hi-res upgrade trả 403/404 → mất ảnh | Trung bình | Trung bình | Fallback chain 4 bước, cuối cùng luôn dùng URL gốc |
| Sticker che mất chi tiết sản phẩm | Cao | Trung bình | Scale mặc định nhỏ (0.15), preview trong UI (Phase 7) trước khi chạy batch |
| Amazon chặn IP khi tải ảnh (khác với scrape) | Thấp | Trung bình | `m.media-amazon.com` là CDN, ít chặn; nếu bị → route qua proxy của provider |
| Upload R2 fail giữa batch → ảnh mồ côi | Trung bình | Thấp | Key deterministic → retry ghi đè đúng chỗ; job dọn object không có DB row (chạy tuần) |

## Security Considerations

- **SSRF:** URL ảnh đến từ scrape response (bên thứ 3), không tin được. Chỉ tải nếu host khớp allowlist: `m.media-amazon.com`, `images-na.ssl-images-amazon.com`, `images-eu.ssl-images-amazon.com`, `*.media-amazon.com`. Chặn IP private trước khi connect.
- **Decompression bomb:** `Image.MAX_IMAGE_PIXELS = 50_000_000` — bắt buộc, không bỏ.
- **Pillow CVE:** pin version, bật Dependabot/`pip-audit` trong CI. Pillow có lịch sử CVE parse ảnh.
- **Không tin `Content-Type`** — chỉ dùng magic bytes để quyết định format.
- R2 bucket **không** bật public listing. Chỉ serve qua custom domain + object key khó đoán (có hash).
- Strip toàn bộ EXIF (có thể chứa GPS, thông tin thiết bị) — `normalize_image` đã làm khi tạo Image mới.

## Next Steps

→ [Phase 4 — Sticker & Template System](./phase-04-sticker-template-system.md) (cung cấp `Layer` schema mà engine này tiêu thụ)
