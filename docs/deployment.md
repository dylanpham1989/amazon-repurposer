# Deploy — Render (free) + Neon Postgres (free)

Tổng chi phí **$0/tháng**. Không cần thẻ tín dụng cho cả hai.

## Vì sao không phải Vercel

Đã đánh giá và loại. Vercel là serverless, app này không hợp:

| Vấn đề | Trên Vercel |
|--------|-------------|
| Job nền `asyncio.create_task` | Function chết khi trả response → batch dừng giữa chừng |
| Cron để gặm queue | Hobby **chỉ chạy 1 lần/ngày** |
| Filesystem | Ephemeral — SQLite và file sticker đều mất |
| State in-memory | Không chia sẻ giữa các instance |

Vercel *có thể* dùng nếu viết lại thành 1 request = 1 sản phẩm (Hobby cho 300s/request
với Fluid Compute), nhưng tốn 6–10h và batch sẽ dừng khi đóng tab.

Hugging Face Spaces cũng đã loại: **Docker Space thành trả phí từ 7/2026**.
Koyeb đã bỏ free compute.

## Đã đổi gì để deploy được

| Trước | Sau | Vì sao |
|-------|-----|--------|
| SQLite (`data/app.db`) | Postgres (asyncpg) | Disk của Render là ephemeral |
| Sticker ở `data/assets/*.png` | Cột `assets.data` BYTEA + cache in-process | Cùng lý do |
| `MAX_EDGE=2000`, 4 luồng | Chỉnh được qua env; deploy dùng 1600 / 2 luồng | Render free chỉ 0.1 CPU / 512 MB |

Placeholder SQL vẫn viết kiểu `?` — `app/db.py` có adapter đổi sang `$n` của Postgres,
rẻ hơn nhiều so với sửa ~50 câu query rải khắp codebase.

## Bước 1 — Neon Postgres

1. https://console.neon.tech → đăng ký (không cần thẻ)
2. Create project → chọn region gần nhất (Singapore nếu ở VN)
3. Copy **Connection string**:
   `postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`

Free: 0.5 GB, 100 compute-hours/tháng, scale-to-zero sau 5 phút (cold start ~0.5–1s).

## Bước 2 — Đẩy code lên GitHub

```bash
git init
git add -A
git commit -m "Amazon Repurposer"
```

Kiểm tra `.env` **không** lọt vào commit:

```bash
git ls-files | grep -c "^\.env$"
```

Phải trả `0`. Rồi tạo repo trên GitHub và push.

## Bước 3 — Render

1. https://render.com → đăng ký (không cần thẻ)
2. **New → Blueprint** → chọn repo → Render đọc `render.yaml`
3. Điền các biến `sync: false` trên dashboard:

| Biến | Lấy từ |
|------|--------|
| `APP_PASSWORD` | tự đặt |
| `DATABASE_URL` | Neon, bước 1 |
| `GEMINI_API_KEY` | Google AI Studio |
| `SCRAPEDO_TOKEN` | dashboard Scrape.do |
| `R2_ACCOUNT_ID` `R2_ACCESS_KEY_ID` `R2_SECRET_ACCESS_KEY` `R2_BUCKET` `R2_PUBLIC_BASE_URL` | Cloudflare |

`SESSION_SECRET` Render tự sinh, không cần điền.

4. Deploy — lần đầu ~5 phút.
5. Mở `https://<tên-app>.onrender.com`

Schema Postgres tự tạo lúc startup (`CREATE TABLE IF NOT EXISTS`), không cần migration thủ công.

## Điều PHẢI biết về Render free

**Spin-down sau 15 phút không có request.** Hệ quả thật:

- Truy cập đầu sau khi ngủ mất **~1 phút** để dậy.
- **Đóng tab giữa lúc batch chạy → service ngủ → batch chết.** Khi dậy lại,
  `recover_stuck_jobs()` đánh dấu sản phẩm dở dang là `failed`, bấm Retry chạy tiếp.
  Không mất dữ liệu, nhưng phải bấm lại.
- **Giữ tab mở khi chạy batch** — HTMX poll 2s/lần nên service không ngủ.

**0.1 CPU** — xử lý ảnh chậm hơn máy local nhiều lần. Đã hạ `IMAGE_MAX_EDGE=1600`
và `WEBP_METHOD=2` để bù. Vẫn chậm thì hạ `IMAGE_CONCURRENCY=1`.

**750 instance-hours/tháng** đủ cho 1 service chạy cả tháng (744h).

## Sau khi deploy

Nút copy hoạt động vì Render cấp HTTPS sẵn (`navigator.clipboard` cần secure context).

```
GET /healthz   -> {"status":"ok"}
GET /readyz    -> ready:true, đủ 4 dependency
```

## Chạy local song song

```bash
docker run -d --name ar-pg -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app \
  -e POSTGRES_DB=app -p 55432:5432 postgres:16-alpine
```

`.env`: `DATABASE_URL=postgresql://app:app@127.0.0.1:55432/app`

Local để mặc định `IMAGE_MAX_EDGE=2000`, `IMAGE_CONCURRENCY=4`, `WEBP_METHOD=4`.

Bonus khi chạy local: IP dân dụng nên tầng fetch trực tiếp pass ~20–25%, tiết kiệm
credit Scrape.do. Trên Render là IP datacenter → gần như luôn phải dùng Scrape.do.
