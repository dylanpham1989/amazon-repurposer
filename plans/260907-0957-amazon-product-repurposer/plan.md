---
title: "Amazon Product Repurposer — bản đơn giản, $0/tháng"
description: "Web app nhận link Amazon, lấy ảnh + title + mô tả, dán sticker lên ảnh lưu Cloudflare R2, viết lại text bằng Gemini free tier. Một process FastAPI, SQLite, không tốn tiền."
status: completed
priority: P1
effort: 38h
issue: null
branch: main
tags: [feature, backend, scraping, ai, mvp]
blockedBy: []
blocks: []
created: 2026-09-07
---

# Amazon Product Repurposer — bản đơn giản

## Overview

Dán link Amazon → lấy toàn bộ ảnh (độ phân giải cao nhất) + title + mô tả →
dán logo/sticker lên ảnh, lưu Cloudflare R2 → viết lại title/mô tả bằng AI, giữ nguyên nội dung.

**Mục tiêu thiết kế: $0/tháng, một process, ít thứ để hỏng nhất có thể.**

## Giả định (nói rõ để mày sửa nếu sai)

1. **Volume: vài chục sản phẩm/ngày**, không phải 2000. Nếu sai thì bảo tao — kiến trúc đổi hẳn.
2. **Một người dùng** (chính mày). Không có đăng ký, không phân quyền. Bảo vệ bằng 1 password trong env.
3. **Chạy trên máy Mac của mày** (Docker hoặc `uv run`). Không phải deploy cloud 24/7.
4. **Bỏ Next.js**, dùng FastAPI + Jinja2 + HTMX — một process, không build step, không `node_modules`.
   Đây là thay đổi so với lựa chọn trước của mày; đổi vì "đơn giản". Muốn quay lại Next.js:
   giữ nguyên Phase 1–4, thay Phase 5 bằng [phase-07 bản cũ](./reference-full-scale/phase-07-frontend-application.md), +8h và 2 deploy.

## Tech Stack — tất cả free

| Lớp | Chọn | Free tier |
|-----|------|-----------|
| App | **FastAPI + Jinja2 + HTMX + CSS tự viết** — 1 process | — |
| DB | **Postgres (Neon)** — đổi từ SQLite khi deploy | 0.5 GB, không cần thẻ |
| Host | **Render** (Docker, free) | 750 giờ/tháng, không cần thẻ |
| Job queue | `asyncio.Task` + bảng `jobs` trong SQLite | — |
| Storage | **Cloudflare R2** | 10 GB, 1M upload/tháng, egress $0 |
| Fetch | **httpx trực tiếp** → fallback **Scrape.do** | 1.000 req/tháng, no card, no expiry |
| Parse HTML | **selectolax** (tự viết selector) | — |
| AI | **Google Gemini Flash** | 1.500 req/ngày, 15 RPM, no card |
| Ảnh | **Pillow** | — |
| Host | Local + `cloudflared tunnel` nếu cần truy cập ngoài | — |

**Chi phí: $0/tháng.** Chi tiết + nguồn: [Research 02](./research/research-02-free-tier-options.md).

## Ba đánh đổi của "free" — biết trước để không bất ngờ

1. **Phải tự viết parser HTML Amazon.** Free nghĩa là không có ai parse hộ. ~150–200 dòng
   selector. Amazon đổi layout → parser vỡ → phải sửa. Giảm thiểu: test bằng fixture HTML
   đã lưu, chạy trong CI, biết vỡ ngay.
2. **Gemini free tier: Google có thể dùng prompt/response của mày để cải thiện sản phẩm họ.**
   Mô tả sản phẩm Amazon vốn public nên phần lớn chấp nhận được — nhưng phải biết.
   Không muốn → OpenAI `gpt-5-nano`, ~$1/tháng ở 100 SP/ngày.
3. **Trần thực tế ~40 SP/ngày.** ĐÃ ĐO 2026-09-07 từ IP này: fetch trực tiếp chỉ pass
   **~20-25%** (Amazon trả trang CAPTCHA 3.781 bytes, HTTP 200). Cookie warmup và xoay
   User-Agent **không cứu được**. Nên ~75-80% request tốn credit Scrape.do
   → 1.000 credit/tháng ≈ **1.250 sản phẩm/tháng ≈ 40/ngày**. Cache 24h đẩy con số này lên.

## ⚠️ Rủi ro pháp lý — vẫn còn nguyên, free hay không

- Amazon ToS **cấm** scraping tự động.
- **Ảnh sản phẩm và mô tả có bản quyền.** Giá/thông số là dữ kiện (không bảo hộ) — ảnh thì có.
- **AI rewrite KHÔNG xoá bản quyền.** Tác phẩm phái sinh vẫn vi phạm.
- Dán logo lên ảnh người khác làm **nặng thêm** vấn đề.

Chỉ dùng cho sản phẩm mày có quyền (của chính mày, hoặc supplier cho phép bằng văn bản).
Nếu là affiliate → đường hợp pháp là Amazon PA-API 5.0 + Associates, ảnh được cấp phép.

Chi tiết: [Research 01 §6](./research/research-01-providers-and-costs.md).

## Phases

| Phase | Name | Effort | Status |
|-------|------|--------|--------|
| 1 | [Khung app & Storage](./phase-01-app-skeleton.md) | 6h | **Done** |
| 2 | [Lấy & Parse dữ liệu Amazon](./phase-02-fetch-and-parse.md) | 8h | **Done** |
| 3 | [Pipeline ảnh & Sticker](./phase-03-image-pipeline.md) | 10h | **Done** |
| 4 | [AI Rewrite bằng Gemini](./phase-04-ai-rewrite.md) | 6h | **Done** |
| 5 | [Giao diện web & Jobs](./phase-05-web-ui-and-jobs.md) | 8h | **Done** |
| 6 | [Deploy — Render + Neon](../../docs/deployment.md) | 4h | **Done** |

## Data Flow

```
Dán link  →  parse URL → ASIN + marketplace
          →  fetch HTML (trực tiếp → Scrape.do nếu bị chặn)
          →  parse selectolax → ProductData
          │
          ├─► Ảnh: nâng hi-res → tải → Pillow dán sticker → WebP → R2
          └─► Text: Gemini 1 lời gọi (title + description) → kiểm tra số liệu
          │
          └─► lưu SQLite → HTMX poll cập nhật UI
```

## Những gì CỐ TÌNH bỏ đi so với bản đầy đủ

Bỏ vì không cần ở quy mô này — không phải quên:

Redis · ARQ worker riêng · Postgres · adapter layer nhiều provider · scrape cache table riêng ·
template versioning · circuit breaker · cost guard · Prometheus metrics · Sentry ·
SSE (dùng HTMX polling) · JWT + multi-user · export CSV cho Shopify/Woo · ZIP streaming ·
Docker multi-stage · CI deploy pipeline.

Bản thiết kế đầy đủ vẫn còn ở [reference-full-scale/](./reference-full-scale/README.md)
— đó là đường nâng cấp khi vượt free tier.

## Những gì GIỮ LẠI dù "đơn giản" — và tại sao

| Giữ | Lý do |
|-----|-------|
| **Nâng ảnh lên hi-res** (~10 dòng) | Ảnh provider trả về là thumbnail 500px. Không có bước này thì cả app vô nghĩa |
| **Kiểm tra số liệu sau rewrite** (~40 dòng) | LLM sẽ bịa thông số. Đăng bán sai kích thước = khách trả hàng. Rẻ và chặn được hại thật |
| **Sticker có điều kiện** (~10 dòng) | Dán "Best Seller" lên sản phẩm không phải best seller là quảng cáo sai sự thật |
| **R2 key theo hash nội dung** (~5 dòng) | Chạy lại không upload trùng → tiết kiệm quota 1M ops free |
| **Cache HTML đã fetch** (~20 dòng) | Quota Scrape.do chỉ 1000/tháng. Cache là thứ bảo vệ nó |

## Kết quả kiểm tra thực tế (2026-09-07)

Chạy `uv run scripts/check_setup.py` — **11/11 pass**. Số liệu đo được, không phải giả định:

| Hạng mục | Kết quả |
|----------|---------|
| Gemini API key | OK, 54 model khả dụng |
| `gemini-flash-latest` / `gemini-3.8-flash` | **503 "high demand"** — alias `-latest` trỏ model mới nhất, cũng là model đông nhất |
| `gemini-3.7-flash` | **OK, 1.9s** ← đang dùng |
| `gemini-flash-lite-latest` | OK, 1.0s, ít token hơn ← fallback |
| R2 bucket / upload / head / public URL / delete | OK toàn bộ |
| Fetch trực tiếp Amazon | **~20-25% pass** — đa số trả CAPTCHA |
| Scrape.do **chế độ thường** (1 credit) | **OK, HTML 2.4 MB hợp lệ** — không cần `super=true` (tốn 5-25x credit) |
| Parse thử fixture | title/price/bullets/description/A+/badge/**26 ảnh hi-res** đều lấy được |

Fixture đã lưu: `data/fixtures/amazon_scrapedo.html`

## Dependencies

Cần đăng ký trước Phase 2 (tất cả đều free, chỉ R2 cần thẻ để verify):
- [ ] [Scrape.do](https://scrape.do) — 1000 req/tháng, không cần thẻ
- [ ] [Google AI Studio](https://aistudio.google.com) — Gemini API key, không cần thẻ
- [ ] [Cloudflare R2](https://dash.cloudflare.com) — bucket + API token (cần thẻ verify, không bị tính tiền trong free tier)
