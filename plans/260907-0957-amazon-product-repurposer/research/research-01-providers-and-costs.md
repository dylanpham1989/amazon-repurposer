# Research 01 — Scraping Providers, Storage, AI Costs

Date: 2026-09-07
Scope: chọn provider scrape Amazon, storage, model AI rewrite; dựng cost model ở mức 2000 sản phẩm/ngày.

## 1. Amazon Scraping API — so sánh

Đo bởi bên thứ 3 (Scrapingdog benchmark, 2026). Giá là **per 1000 requests**, giảm dần theo volume.

| Provider | Giá / 1K req | Response time | Success rate | Ghi chú |
|----------|--------------|---------------|--------------|---------|
| **Scrapingdog** | $0.20 → $0.063 | 3.55s | 100% | Rẻ nhất, nhanh nhất trong benchmark. Hỗ trợ postal-code targeting mọi quốc gia. |
| **Oxylabs** Web Scraper API | $1.35 → $1.25 (một số nguồn: $0.50/1K ở gói Micro $49/mo) | 5.38s | 100% | Docs tốt nhất, parser `amazon_product` chín, có async + webhook. Enterprise-grade. |
| **Bright Data** | $0.90 flat | ~9s | 100% | Webhook-based, docs khó. Free tier có. |
| **ScraperAPI** | $2.45 → $0.475 | 9.61s | 100% | Đắt nhất entry, chậm. Postal-code chỉ US. |
| **Rainforest API** | $8.30 (gói $83/mo, 10K credits) | — | — | Đắt nhất. Bù lại: parser Amazon chuyên biệt, schema giàu nhất, có A+ content. |
| **SocialCrawl** | $0.01/req PAYG, 100 free/mo | — | — | Rẻ, nhưng nhỏ/mới → rủi ro vendor. |

### Kết luận
- **Primary: Oxylabs** — schema parse ổn định nhất cho `amazon_product`, docs rõ, có async job + webhook (khớp kiến trúc queue). Chi phí ở 60K req/tháng ≈ **$30–75/tháng**.
- **Secondary (fallback + cost optimization): Scrapingdog** — rẻ hơn 5–6 lần. Bật khi volume tăng.
- Vì vậy plan bắt buộc có **provider adapter layer** (Phase 2). Đây không phải over-engineering: giá chênh 6x và schema mỗi bên khác nhau → swap provider là bài toán "khi nào", không phải "có hay không".

### Cảnh báo về field `description`
Không provider nào đảm bảo 100% trả về **full product description**. Amazon có 3 nguồn text khác nhau:
1. `feature_bullets` — 5–10 gạch đầu dòng. **Luôn có.** Đây là nguồn chính đáng tin.
2. `product_description` — đoạn text dài. **Thường có, không phải luôn luôn.**
3. `aplus_content` (A+ / Enhanced Brand Content) — HTML + ảnh. **Chỉ brand đăng ký mới có.** Nhiều provider không parse.

→ Pipeline phải **fallback theo thứ tự**: `product_description` → nếu rỗng thì ghép `feature_bullets` → nếu vẫn rỗng thì strip text từ `aplus_content`. Nếu cả 3 rỗng → đánh dấu `description_missing`, không cho AI bịa.

## 2. Ảnh Amazon — nâng độ phân giải

URL ảnh Amazon có dạng:
```
https://m.media-amazon.com/images/I/71AbCdEfGhL._AC_SL1500_.jpg
                                    ^^^^^^^^^^^^ ^^^^^^^^^^^^
                                    image id     modifier block
```

Bỏ toàn bộ modifier block → ảnh gốc, độ phân giải cao nhất:
```
https://m.media-amazon.com/images/I/71AbCdEfGhL.jpg
```

Regex: `r'\._[A-Za-z0-9_,\-]+_\.'` → thay bằng `'.'`

**Quan trọng:** không phải lúc nào cũng thành công. Phải:
1. Thử URL đã strip trước, `HEAD` request kiểm tra 200 + `content-length` > URL gốc.
2. Nếu fail → thử `._SL1600_.` → `._SL1500_.`
3. Nếu vẫn fail → dùng URL gốc provider trả về.

Ảnh gốc thường 1500–3000px, 200KB–2MB. Có ảnh là PNG trong suốt, có ảnh CMYK JPEG → phải normalize sang RGB trước khi xử lý.

## 3. Storage — Cloudflare R2 (đã chốt)

| Khoản | Giá |
|-------|-----|
| Storage Standard | $0.015 / GB-tháng |
| Storage Infrequent Access | $0.010 / GB-tháng |
| Class A ops (PUT, LIST, COPY) | $4.50 / triệu |
| Class B ops (GET, HEAD) | $0.36 / triệu |
| **Egress** | **$0 — miễn phí toàn bộ** |
| Free tier | 10 GB storage, 1M Class A, 10M Class B / tháng |

Egress $0 là lý do chọn R2: ảnh sản phẩm được load lại rất nhiều lần (frontend preview, khách hàng cuối, marketplace crawl). DO Spaces tính $0.01/GB sau 1TB.

**Rủi ro chính: storage tích lũy.** Xem cost model bên dưới → bắt buộc có lifecycle policy.

## 4. AI Rewrite — OpenAI

| Model | Input / 1M tok | Output / 1M tok |
|-------|----------------|-----------------|
| gpt-5-mini | $0.25 | $2.00 |
| gpt-5-nano | $0.05 | $0.40 |

**Lưu ý vòng đời:** gpt-5-mini có lịch shutdown **11/12/2026**, thay bằng gpt-5.6-terra.
→ Model name **phải nằm trong env var**, không hardcode. Phase 5 bắt buộc có model registry + fallback chain.

**Chọn model:** `gpt-5-mini` cho description (cần giữ chính xác chi tiết), `gpt-5-nano` cho title (ngắn, đơn giản). Cấu hình được per-field.

## 5. Cost model — 2000 sản phẩm/ngày (60.000/tháng)

Giả định: 7 ảnh/sản phẩm, ảnh sau xử lý ~150KB WebP.

| Hạng mục | Cách tính | $/tháng |
|----------|-----------|---------|
| Scraping (Oxylabs) | 60K req × $0.50–1.25/1K | $30 – $75 |
| OpenAI (mini cho desc, nano cho title) | ~1.5K in + 0.8K out per SP | ~$110 |
| OpenAI (nano cho cả hai) | " | ~$25 |
| R2 storage — **tháng 1** | 60K × 7 × 150KB = 63 GB | $0.95 |
| R2 storage — **tháng 12 (tích lũy, không xoá)** | 756 GB | $11.34 |
| R2 Class A (upload) | 420K PUT | $1.89 |
| R2 egress | — | **$0** |
| Railway (API + worker + Postgres + Redis) | | $20 – $40 |
| **TỔNG tháng 1** | | **~$80 – $230** |

Biến số lớn nhất là OpenAI (chênh 4x giữa mini và nano) và scraping. Cả hai đều cần **cost tracking per job** (Phase 5, Phase 8).

**Lifecycle policy bắt buộc:** không có nó, storage tăng tuyến tính mãi mãi. Đề xuất: ảnh của job chưa export sau 90 ngày → chuyển Infrequent Access; sau 180 ngày → xoá. Job đã export/đánh dấu `keep` → giữ vĩnh viễn.

## 6. Pháp lý — phải đọc, không được bỏ qua

Đây là rủi ro thật, không phải formality.

1. **Amazon ToS cấm scraping tự động**, kể cả dữ liệu công khai. Amazon có quyền ban và kiện.
2. **Ảnh sản phẩm và mô tả được bảo hộ bản quyền.** Đây là điểm khác biệt then chốt: giá và thông số kỹ thuật là **dữ kiện** (không được bảo hộ), nhưng **ảnh là tác phẩm sáng tạo (được bảo hộ)**.
3. Bán lại dữ liệu Amazon thô là cách nhanh nhất nhận thư luật sư.
4. AI rewrite mô tả **không xoá được bản quyền** — tác phẩm phái sinh vẫn là vi phạm nếu bản gốc có bản quyền.
5. Dán logo/sticker lên ảnh gốc của người khác **làm nặng thêm** vấn đề, không nhẹ đi.

### Hệ quả với thiết kế
- Chỉ dùng cho sản phẩm **mày có quyền** (sản phẩm của chính mày trên Amazon, hoặc supplier/brand cho phép bằng văn bản).
- Nếu dùng cho affiliate: Amazon **có** chương trình hợp pháp là PA-API 5.0 + Associates. Ảnh dùng qua PA-API được cấp phép cho mục đích affiliate.
- **Hệ thống phải có trường `source_authorization`** trên mỗi product: `owned` / `supplier_licensed` / `affiliate_paapi` / `unverified`. Mặc định `unverified`, và UI phải cảnh báo rõ.
- Không xây tính năng public-share ảnh đã xử lý cho bên thứ 3 ở MVP.

## Nguồn

- [4 Best Amazon Scraping APIs in 2026 — Scrapingdog](https://www.scrapingdog.com/blog/best-amazon-scraping-apis/)
- [Best Amazon Scraper APIs for 2026 — Oxylabs](https://oxylabs.io/blog/best-amazon-scraper-api)
- [2026's Top Amazon Scraper APIs — Scrape.do](https://scrape.do/blog/best-amazon-scraper-api/)
- [Cloudflare R2 Pricing — developers.cloudflare.com](https://developers.cloudflare.com/r2/pricing)
- [OpenAI API Pricing 2026 — pricepertoken.com](https://pricepertoken.com/pricing-page/provider/openai)
- [GPT-5 Mini API Cost Breakdown 2026](https://www.getapipulse.com/blog-gpt5-mini-cost-breakdown.html)
- [Amazon Product Image Extraction: URL Patterns — EasyParser](https://easyparser.com/blog/amazon-product-image-extraction)
- [Is Scraping Amazon Legal? What Courts and ToS Really Say — Thunderbit](https://thunderbit.com/blog/is-scraping-amazon-legal)
- [Amazon Conditions of Use](https://www.amazon.com/gp/help/customer/display.html?nodeId=GLSBYFE9MGKKQXXM)
