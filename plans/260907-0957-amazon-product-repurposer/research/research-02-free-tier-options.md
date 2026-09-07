# Research 02 — Phương án Free (đã verify)

Date: 2026-09-07
Scope: tìm cấu hình $0/tháng cho app đơn giản, low volume.

## Nguyên tắc chọn

Ưu tiên: **free vĩnh viễn > free trial**, **không cần thẻ tín dụng**, **không spin-down**.

## 1. Lấy dữ liệu Amazon

### ⚠️ Đính chính một thông tin sai phổ biến
Nhiều blog so sánh ghi "WebScrapingAPI free 5000 requests/tháng". Tao đã kiểm tra trang
pricing gốc: **5000 free là cho Scraper API generic (trả HTML thô)**. Riêng
**Data API (Amazon có parser JSON) chỉ free 100 request**. Đừng tin con số 5000 cho Amazon.

### So sánh free tier thật

| Provider | Free tier | Cần thẻ? | Hết hạn? | Trả gì |
|----------|-----------|----------|----------|--------|
| **Scrape.do** | **1.000 req/tháng** | **Không** | **Không** | HTML qua residential proxy + CAPTCHA handling, 5 concurrent |
| ScraperAPI | 1.000/tháng (sau trial 7 ngày 5.000) | Có | Không | HTML / JSON |
| Amazon Scraper API | 1.000 lúc đăng ký | Không | Một lần | JSON |
| WebScrapingAPI (Data API) | 100/tháng | ? | ? | JSON Amazon |
| FlyByAPIs | 100/tháng | Không | Không | JSON |
| Apify | $5 credit/tháng | Không | Không | tuỳ actor |

### Kết luận: chiến lược 2 tầng

```
1. Fetch trực tiếp bằng httpx (headers giống trình duyệt thật)   → FREE, không giới hạn
   ↓ nếu bị chặn (403 / CAPTCHA page / thiếu block sản phẩm)
2. Fetch lại qua Scrape.do                                        → 1.000/tháng free
```

Tầng 1 hoạt động **tốt hơn nhiều khi chạy từ IP nhà (residential)** — Amazon chặn
IP datacenter rất mạnh nhưng khoan dung với IP dân dụng ở volume thấp. Đây là lý do
kỹ thuật thật để chạy app trên máy Mac thay vì host cloud.

**Cái giá của free:** tầng 1 trả HTML thô → **phải tự viết parser**. Khoảng 150–200 dòng
`selectolax` selector. Amazon đổi layout thì parser vỡ, phải sửa. Đây là đánh đổi trực tiếp:
tiết kiệm $30–75/tháng, trả bằng công bảo trì parser.

Giảm thiểu: test parser bằng fixture HTML đã lưu, chạy trong CI → biết vỡ ngay khi vỡ.

## 2. AI Rewrite — Google Gemini free tier

| | Gemini Flash (free) | OpenAI gpt-5-nano |
|---|---|---|
| Giá | **$0** | $0.05 / $0.40 per 1M tok |
| Giới hạn | 15 RPM · 250K TPM · **1.500 request/ngày** | theo tier |
| Cần thẻ | **Không** | Có |
| Context | 1M token | — |

**1.500 request/ngày** — nếu gộp title + description vào **một** lời gọi (structured output
2 field) thì được **1.500 sản phẩm/ngày miễn phí**. Thừa sức cho app đơn giản.

### ⚠️ Đánh đổi phải biết
Free tier của Gemini: **Google có thể dùng prompt và response của mày để cải thiện sản phẩm
của họ.** Với mô tả sản phẩm Amazon (vốn đã public) thì phần lớn là chấp nhận được — nhưng
phải biết để tự quyết. Muốn tránh → trả tiền tier, hoặc dùng OpenAI nano (~$25/tháng ở
2000 SP/ngày, ~$1/tháng ở 100 SP/ngày).

Free tier chỉ có model **Flash / Flash-Lite**. Pro là trả tiền. Flash đủ tốt cho paraphrase.

## 3. Storage — Cloudflare R2 free tier

| | Free tier/tháng |
|---|---|
| Storage | **10 GB** |
| Class A ops (upload) | **1 triệu** |
| Class B ops (đọc) | **10 triệu** |
| Egress | **$0 không giới hạn** |

10 GB ≈ **66.000 ảnh WebP** (150KB/ảnh) ≈ 9.400 sản phẩm (7 ảnh/SP).
Ở 30 SP/ngày thì hơn 10 tháng mới đầy. Vẫn nên có nút xoá batch cũ.

Cần thẻ để tạo tài khoản R2 (Cloudflare verify) nhưng không bị tính tiền trong free tier.

## 4. Database

| | Free tier | Bẫy |
|---|---|---|
| **SQLite (file)** | **Tuyệt đối free** | Không dùng được trên host có disk ephemeral (Render free) |
| Supabase | 500MB DB, 5GB egress | **Pause sau 1 tuần không hoạt động** |
| Neon | 0.5GB/project, 100 CU-h | Scale-to-zero (cold start) |
| Render Postgres | — | **Hết hạn sau 30 ngày** |
| Railway | credit dùng 1 lần | Không có free tier thật |

→ Chạy local: **SQLite**. Không cần server, không cần Docker cho DB, backup = copy 1 file.

## 5. Hosting

| | Free? | Bẫy |
|---|---|---|
| **Chạy local (Mac)** | **$0 tuyệt đối** | Chỉ chạy khi máy bật |
| Render | Có, 512MB/0.1 CPU | **Spin-down sau 15 phút**, cold start ~1 phút. Disk ephemeral → SQLite mất dữ liệu |
| Fly.io | **Không còn free tier** (2026) | Trial 2 VM-hours / 7 ngày, cần thẻ |
| HuggingFace Spaces | Có, 2vCPU/16GB | Không cam kết uptime, public mặc định |
| Vercel | Có (Next.js) | Serverless — không hợp job chạy dài |

### Khuyến nghị: chạy local trước
Không chỉ vì free. Ba lý do kỹ thuật thật:
1. **IP dân dụng** → tầng fetch trực tiếp hoạt động tốt hơn hẳn → tiết kiệm quota Scrape.do
2. **Không spin-down** → job 5 phút không bị cắt giữa chừng
3. **SQLite dùng được** → không cần DB server

Muốn truy cập từ xa: `cloudflared tunnel` (free) mở localhost ra internet có HTTPS.
Deploy lên Render sau chỉ cần đổi `DATABASE_URL` sang Supabase.

## 6. Tổng kết cấu hình $0

| Lớp | Chọn | Giới hạn free |
|-----|------|---------------|
| App | FastAPI + Jinja2 + HTMX (1 process) | — |
| DB | SQLite | — |
| Queue | asyncio task + bảng jobs | — |
| Storage | Cloudflare R2 | 10 GB, 1M upload/tháng |
| Fetch | httpx trực tiếp → Scrape.do fallback | 1.000 req/tháng |
| Parse | selectolax (tự viết) | — |
| AI | Gemini Flash | 1.500 req/ngày, 15 RPM |
| Ảnh | Pillow | — |
| Host | Local Docker / cloudflared tunnel | — |

**Chi phí: $0/tháng.** Trần thực tế: **~33 sản phẩm/ngày** (giới hạn Scrape.do 1000/tháng
là nút thắt hẹp nhất) — nhưng nếu fetch trực tiếp thành công phần lớn thì cao hơn nhiều,
Scrape.do chỉ dùng khi bị chặn.

Vượt trần thì nâng cấp rẻ nhất theo thứ tự:
1. Scrapingdog $0.20/1K request (rẻ nhất thị trường) — bỏ được parser tự viết luôn
2. Gemini paid tier hoặc OpenAI nano
3. R2 $0.015/GB-tháng

## Nguồn

- [Scrape.do Pricing](https://scrape.do/pricing/) — 1.000 free/tháng, no card, no expiry
- [WebScrapingAPI Pricing](https://www.webscrapingapi.com/pricing) — đính chính: Data API free 100
- [Gemini API Free Tier 2026 — TokenMix](https://tokenmix.ai/blog/gemini-api-free-tier-limits)
- [Gemini API Free Tier Rate Limits — aipromptshub](https://aipromptshub.co/blog/gemini-api-free-tier-rate-limits)
- [Cloudflare R2 Pricing](https://developers.cloudflare.com/r2/pricing)
- [Free PostgreSQL Hosting 2026 — Swyftstack](https://swyftstack.com/blog/free-postgresql-hosting)
- [Platforms with a real free tier 2026 — Render](https://render.com/articles/platforms-with-a-real-free-tier-for-developers-in-2026)
- [Fly.io Free Tier 2026](https://www.saaspricepulse.com/blog/flyio-free-tier-2026)
