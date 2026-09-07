# Amazon Product Repurposer

Dán link Amazon → lấy toàn bộ ảnh ở độ phân giải cao nhất + tiêu đề + mô tả →
dán logo/sticker lên ảnh, lưu Cloudflare R2 → viết lại tiêu đề/mô tả bằng AI, giữ nguyên nội dung.

Thiết kế để chạy **$0/tháng**: SQLite + Cloudflare R2 free tier + Gemini free tier + Scrape.do free tier.

## Chạy local

Cần Postgres (app đã chuyển từ SQLite sang Postgres để deploy được):

```bash
docker run -d --name ar-pg -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app \
  -e POSTGRES_DB=app -p 55432:5432 postgres:16-alpine
```

`.env`: `DATABASE_URL=postgresql://app:app@127.0.0.1:55432/app`

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8787
```

## Deploy

Render (free) + Neon Postgres (free) — xem [docs/deployment.md](docs/deployment.md).
**Không deploy được lên Vercel**: job nền và filesystem không hợp serverless, lý do chi tiết ở cùng file đó.

Mở http://localhost:8787 — đăng nhập bằng `APP_PASSWORD` trong `.env`.

> **Phải dùng `localhost`, không dùng IP LAN (`192.168.x.x`).**
> `navigator.clipboard` chỉ chạy trên HTTPS hoặc localhost — mở qua IP LAN thì
> toàn bộ nút copy sẽ chết. Muốn truy cập từ máy khác: `cloudflared tunnel` (có HTTPS).

## Tính năng

| Trang | Làm gì |
|-------|--------|
| `/` | Dán link → xử lý → xem kết quả ngay: title cũ/mới, mô tả cũ/mới, bảng ảnh ghép cặp gốc↔mới. **Mọi ô đều có nút copy.** Sửa tay hoặc viết lại từng sản phẩm. |
| `/templates` | Kho sticker (upload PNG có alpha) + editor: lưới 9 vị trí, slider kích thước/lề/độ đục/xoay, **preview live** |
| `/batches` | Lịch sử, export JSON / CSV / ZIP ảnh, xoá batch (xoá cả ảnh trên R2) |
| `/quota` | Scrape.do (tháng) · Gemini (ngày) · R2 (GB) |

## Kiểm tra cấu hình

```bash
uv run scripts/check_setup.py
```

Verify cả 4: biến môi trường, Gemini, R2 (upload/đọc/xoá thật), fetch Amazon 2 tầng.

## Test

```bash
uv run pytest -q
uv run ruff check app/
```

## Backup

Dữ liệu nằm trong Postgres:

```bash
pg_dump "$DATABASE_URL" > backup.sql
```

Ảnh nằm trên Cloudflare R2, không nằm trong backup này.

## Giới hạn free tier

| Dịch vụ | Free tier | Nút thắt |
|---------|-----------|----------|
| Scrape.do | 1.000 request/tháng | **Hẹp nhất** — fetch trực tiếp chỉ pass ~20-25% nên đa số request tốn credit |
| Gemini Flash | 1.500 request/ngày, 15 RPM | Gộp title+mô tả vào 1 lời gọi → 1.500 sản phẩm/ngày |
| Cloudflare R2 | 10 GB, 1M upload/tháng, egress $0 | ~66.000 ảnh |

→ Trần thực tế **~40 sản phẩm/ngày**. Cache HTML 24h đẩy con số này lên.

## Lưu ý quan trọng

**Gemini free tier: Google có thể dùng prompt và response để cải thiện sản phẩm của họ.**
Mô tả sản phẩm Amazon vốn public nên phần lớn chấp nhận được. Không muốn thì đổi sang
OpenAI `gpt-5-nano` (~$1/tháng ở 100 sản phẩm/ngày) — chỉ sửa `app/services/rewrite.py`.

**Không dùng alias `gemini-flash-latest`.** Nó trỏ model mới nhất, cũng là model đông nhất,
trả 503 "high demand" liên tục. Đã pin `gemini-3.7-flash` trong `.env`.

## ⚠️ Pháp lý — đọc trước khi dùng

- Amazon ToS **cấm** scraping tự động, kể cả dữ liệu công khai.
- **Ảnh sản phẩm và mô tả có bản quyền.** Giá và thông số kỹ thuật là *dữ kiện*
  (không được bảo hộ), nhưng **ảnh là tác phẩm sáng tạo và được bảo hộ**.
- **AI viết lại KHÔNG xoá được bản quyền.** Tác phẩm phái sinh vẫn là vi phạm.
- Dán logo lên ảnh của người khác làm **nặng thêm** vấn đề, không nhẹ đi.

Chỉ dùng cho sản phẩm bạn có quyền: sản phẩm của chính bạn, hoặc supplier/brand
cho phép bằng văn bản. Nếu làm affiliate, đường hợp pháp là **Amazon PA-API 5.0 +
Associates** — ảnh được cấp phép cho mục đích affiliate.

## Kế hoạch

`plans/260907-0957-amazon-product-repurposer/` — 5 phase.
Bản thiết kế quy mô lớn (khi vượt free tier) ở `reference-full-scale/`.
