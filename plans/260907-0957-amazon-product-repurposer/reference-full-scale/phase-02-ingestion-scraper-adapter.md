# Phase 2 — Ingestion & Scraper Adapter

**Priority:** P1 · **Effort:** 14h · **Status:** Pending
**Context:** [plan.md](./plan.md) · [Phase 1](./phase-01-foundation-infrastructure.md) · [Research 01 §1–2](./research/research-01-providers-and-costs.md)

## Overview

Biến link Amazon thô thành `ProductData` chuẩn hoá. Gồm: parse URL → ASIN, adapter layer cho scraping provider, normalizer, cache, retry.

## Key Insights

- **Adapter layer không phải over-engineering.** Giá giữa provider chênh **6x** (Scrapingdog $0.20 vs ScraperAPI $2.45 / 1K) và schema mỗi bên khác nhau hoàn toàn. Swap là bài toán "khi nào".
- **Không provider nào đảm bảo có `description`.** Amazon có 3 nguồn text độc lập → phải fallback chain, xem §Normalizer.
- **Cache tiết kiệm tiền thật.** User sẽ paste lại cùng link (thử template khác, rewrite lại). Cache 24h cắt được đáng kể chi phí scrape.
- URL Amazon có **rất nhiều biến thể** — shortlink `amzn.to`, `/dp/`, `/gp/product/`, `/gp/aw/d/`, mobile, có/không slug, query param rác. Parser phải chịu được hết.
- Amazon có **~20 marketplace** với TLD khác nhau. ASIN giống nhau nhưng nội dung/giá khác theo marketplace → key cache phải là `(asin, marketplace)`.

## Requirements

### Functional
- Parse mọi biến thể URL Amazon phổ biến → `(asin, marketplace)`
- Resolve shortlink `amzn.to` / `a.co` (follow redirect, max 3 hop)
- Nhận trực tiếp ASIN thô (10 ký tự) + marketplace mặc định
- Adapter interface với ≥2 implementation (Oxylabs, Scrapingdog) + `MockProvider` cho test
- Normalize response → `ProductData` Pydantic model thống nhất
- Cache theo `(asin, marketplace, provider)`, TTL cấu hình được
- Retry với exponential backoff + jitter cho lỗi tạm thời

### Non-functional
- Không bao giờ đưa credential provider vào log
- Mọi lỗi provider map sang error code nội bộ ổn định (frontend không phụ thuộc text lỗi của provider)
- Adapter phải test được **không cần** gọi mạng thật (dùng `respx` + fixture JSON thật đã lưu)

## Architecture

### URL Parser

```python
# services/url_parser.py

AMAZON_DOMAINS = {
    "amazon.com": "US", "amazon.co.uk": "UK", "amazon.de": "DE",
    "amazon.fr": "FR", "amazon.it": "IT", "amazon.es": "ES",
    "amazon.co.jp": "JP", "amazon.ca": "CA", "amazon.com.au": "AU",
    "amazon.in": "IN", "amazon.com.mx": "MX", "amazon.com.br": "BR",
    "amazon.nl": "NL", "amazon.se": "SE", "amazon.pl": "PL",
    "amazon.sg": "SG", "amazon.ae": "AE", "amazon.sa": "SA",
    "amazon.com.tr": "TR", "amazon.com.be": "BE", "amazon.eg": "EG",
}
SHORTENERS = {"amzn.to", "amzn.eu", "a.co", "amzn.asia"}

ASIN_RE = re.compile(r"\b(B[0-9A-Z]{9}|[0-9]{9}[0-9X])\b")
PATH_PATTERNS = [
    r"/dp/([A-Z0-9]{10})",
    r"/gp/product/([A-Z0-9]{10})",
    r"/gp/aw/d/([A-Z0-9]{10})",
    r"/gp/offer-listing/([A-Z0-9]{10})",
    r"/product/([A-Z0-9]{10})",
    r"/-/[a-z]{2}/dp/([A-Z0-9]{10})",   # locale-prefixed: /-/en/dp/XXX
    r"/d/([A-Z0-9]{10})",
]
```

Thứ tự xử lý:
1. Trim, bỏ ký tự vô hình. Nếu chuỗi khớp `^[A-Z0-9]{10}$` → coi là ASIN thô, dùng marketplace mặc định.
2. Nếu host thuộc `SHORTENERS` → `HEAD` follow redirect (timeout 5s, max 3 hop) → dùng URL cuối.
3. Xác định marketplace từ host (bỏ `www.`, `smile.`, `m.`).
4. Thử `PATH_PATTERNS` theo thứ tự trên path.
5. Fallback: query param `?asin=` hoặc `?ASIN=`.
6. Fallback cuối: `ASIN_RE` trên toàn URL. **Cảnh báo:** dễ false-positive → chỉ dùng khi các bước trên fail, và đánh dấu `confidence="low"`.
7. Không tìm được → `InvalidUrlError` với message chỉ rõ URL nào lỗi (hiển thị cho user).

**Validate ASIN:** đúng 10 ký tự, `[A-Z0-9]`, không phải toàn số trừ khi là ISBN-10 (sách).

### Provider Adapter Interface

```python
# services/scraper/base.py
from abc import ABC, abstractmethod

class ScrapeError(Exception):
    """code: not_found | blocked | rate_limited | provider_error | timeout | invalid_response"""
    def __init__(self, code: str, message: str, retryable: bool): ...

class ScraperProvider(ABC):
    name: str
    cost_per_request_usd: float

    @abstractmethod
    async def fetch_product(
        self, asin: str, marketplace: str
    ) -> RawScrapeResult: ...
        # RawScrapeResult = {payload: dict, provider: str, cost_usd: float}

    @abstractmethod
    def normalize(self, payload: dict) -> ProductData: ...
```

Implementation:
- `OxylabsProvider` — POST `https://realtime.oxylabs.io/v1/queries`, body `{"source": "amazon_product", "query": asin, "domain": <tld>, "parse": true}`, Basic Auth. Response ở `results[0].content`.
- `ScrapingdogProvider` — GET `https://api.scrapingdog.com/amazon/product` với `api_key`, `asin`, `domain`.
- `MockProvider` — đọc fixture JSON từ `tests/fixtures/`. Dùng cho dev không tốn tiền (`SCRAPER_PROVIDER=mock`).

Factory: `get_provider(name) -> ScraperProvider`, chọn theo `settings.scraper_provider`.

### Normalizer — `ProductData`

```python
class ProductImage(BaseModel):
    url: str
    position: int
    variant: str | None = None      # MAIN | PT01 | ... nếu provider có

class ProductData(BaseModel):
    asin: str
    marketplace: str
    title: str
    description: str                 # có thể rỗng
    description_source: str          # product_description | feature_bullets | aplus | none
    bullets: list[str] = []
    images: list[ProductImage] = []
    brand: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    rating: float | None = None
    review_count: int | None = None
    is_best_seller: bool = False     # dùng cho conditional sticker (Phase 4)
    is_amazon_choice: bool = False
    has_free_delivery: bool = False
    warnings: list[str] = []
```

**Fallback chain cho `description` — bắt buộc:**
1. `product_description` (nếu có và độ dài ≥ 50 ký tự) → `description_source="product_description"`
2. Ghép `feature_bullets` thành đoạn văn → `description_source="feature_bullets"`
3. Strip HTML từ `aplus_content`, lấy text ≥ 50 ký tự → `description_source="aplus"`
4. Rỗng → `description_source="none"`, thêm `warnings=["description_missing"]`

**Tuyệt đối không** để AI tự bịa description khi source rỗng (Phase 5 phải kiểm tra `description_source != "none"`).

**Chuẩn hoá ảnh:**
- Dedupe theo image ID (phần `71AbCdEfGhL` trong URL) — cùng ảnh có nhiều size khác nhau.
- Loại ảnh placeholder (Amazon dùng `.gif` 1x1, hoặc `transparent-pixel`).
- Loại ảnh của **variant khác** nếu provider trộn vào — chỉ giữ ảnh thuộc ASIN đang hỏi.
- Sắp `position`: ảnh MAIN = 0, còn lại theo thứ tự provider trả về.
- Cắt tối đa `settings.max_images_per_product` (mặc định 15).

### Cache

```python
async def get_or_fetch(asin, marketplace, provider, *, force_refresh=False) -> ProductData:
    if not force_refresh:
        row = await cache.get(asin, marketplace, provider.name)
        if row and row.expires_at > utcnow():
            return provider.normalize(row.payload)   # normalize lại, không cache ProductData
    raw = await provider.fetch_product(asin, marketplace)
    await cache.upsert(asin, marketplace, provider.name, raw.payload,
                       expires_at=utcnow() + timedelta(hours=settings.scrape_cache_ttl_hours))
    return provider.normalize(raw.payload)
```

Cache **payload thô**, không cache `ProductData`. Lý do: sửa normalizer thì cache cũ vẫn dùng được, không phải scrape lại.

Cron dọn cache hết hạn: `DELETE FROM scrape_cache WHERE expires_at < now()` mỗi giờ (ARQ cron job).

### Retry

```python
RETRYABLE = {"rate_limited", "timeout", "provider_error"}
# attempt delays: 2s, 6s, 15s (+ jitter ±30%), max 3 attempts
# "blocked" → retry 1 lần với provider fallback (nếu cấu hình), rồi fail
# "not_found" → KHÔNG retry, fail ngay
```

## Related Code Files

**Create:**
- `apps/api/src/app/services/url_parser.py`
- `apps/api/src/app/services/scraper/__init__.py`
- `apps/api/src/app/services/scraper/base.py`
- `apps/api/src/app/services/scraper/oxylabs.py`
- `apps/api/src/app/services/scraper/scrapingdog.py`
- `apps/api/src/app/services/scraper/mock.py`
- `apps/api/src/app/services/scraper/factory.py`
- `apps/api/src/app/services/scrape_cache.py`
- `apps/api/src/app/schemas/product.py`  (`ProductData`, `ProductImage`)
- `apps/api/tests/fixtures/oxylabs_*.json`  (≥5 sản phẩm thật khác loại)
- `apps/api/tests/test_url_parser.py`
- `apps/api/tests/test_scraper_oxylabs.py`
- `apps/api/tests/test_normalizer.py`

**Modify:**
- `apps/api/src/app/config.py` — đã có sẵn field từ Phase 1

## Implementation Steps

1. **URL parser** — viết trước, test trước. Đây là nơi bug ẩn nhiều nhất.
2. **Thu thập fixture thật** — gọi Oxylabs thủ công cho 5–8 ASIN đa dạng, lưu JSON vào `tests/fixtures/`:
   - 1 sản phẩm có A+ content
   - 1 sản phẩm **không có** `product_description` (chỉ bullets)
   - 1 sản phẩm nhiều variant (màu/size)
   - 1 sản phẩm 1 ảnh duy nhất
   - 1 sản phẩm >10 ảnh
   - 1 sách (ASIN dạng ISBN)
   - 1 marketplace non-US (amazon.de) để test ký tự Unicode
3. **`base.py`** — `ScraperProvider` ABC, `ScrapeError`, `RawScrapeResult`.
4. **`OxylabsProvider`** — fetch + map lỗi HTTP sang error code. Timeout 45s (Oxylabs 5.4s trung bình nhưng đuôi dài).
5. **Normalizer cho Oxylabs** — implement fallback chain description + dedupe ảnh. Viết test dựa trên fixture.
6. **`ScrapingdogProvider`** + normalizer riêng. Chứng minh interface đủ tổng quát.
7. **`MockProvider`** đọc fixture theo ASIN.
8. **Factory** + wiring vào config.
9. **Cache layer** + ARQ cron dọn expired.
10. **Retry wrapper** — dùng `tenacity` hoặc tự viết (khuyến nghị tự viết, ~30 dòng, kiểm soát tốt hơn).
11. **CLI dev tool**: `python -m app.cli scrape <url>` in ra `ProductData` — cực hữu ích khi debug.

## Todo List

- [ ] `url_parser.py` + test ≥30 case URL (gồm shortlink, locale prefix, mobile, query rác)
- [ ] Thu thập ≥7 fixture JSON thật, đa dạng
- [ ] `ScraperProvider` ABC + `ScrapeError` + error code mapping
- [ ] `OxylabsProvider.fetch_product`
- [ ] `OxylabsProvider.normalize` + fallback chain description
- [ ] Dedupe + filter + sort ảnh
- [ ] `ScrapingdogProvider` (fetch + normalize)
- [ ] `MockProvider`
- [ ] `factory.get_provider()`
- [ ] `scrape_cache.get_or_fetch()` + upsert
- [ ] ARQ cron dọn cache hết hạn
- [ ] Retry với backoff + jitter, phân biệt retryable
- [ ] CLI `scrape <url>` để debug
- [ ] Test: parser, normalizer (mọi fixture), retry logic, cache hit/miss

## Success Criteria

- [ ] `pytest tests/test_url_parser.py` — 100% case pass, gồm cả case phải fail rõ ràng
- [ ] Chạy `python -m app.cli scrape <link thật>` trả về `ProductData` đầy đủ ảnh + title + description
- [ ] Với fixture "không có product_description" → `description_source == "feature_bullets"`, description không rỗng
- [ ] Với fixture nhiều size trùng → ảnh đã dedupe, không có 2 entry cùng image ID
- [ ] Đổi `SCRAPER_PROVIDER=scrapingdog` → cùng ASIN trả `ProductData` tương đương (title giống, số ảnh chênh ≤2)
- [ ] Cache hit lần 2 → không có HTTP request ra provider (assert bằng `respx`)
- [ ] Lỗi `not_found` → không retry (assert số lần gọi = 1)
- [ ] Coverage service layer ≥ 85%

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| Provider đổi schema response → normalizer vỡ | **Cao** | Cao | Fixture-based test chạy trong CI; `raw_payload` lưu DB để reprocess; alert khi tỉ lệ `description_source=="none"` tăng đột biến |
| ASIN parse sai (false positive từ regex fallback) | Trung bình | Trung bình | Đánh dấu `confidence="low"`, UI hiện cảnh báo; validate ASIN tồn tại qua kết quả scrape |
| Provider trả `blocked` hàng loạt (Amazon siết) | Trung bình | **Cao** | Fallback provider thứ 2 cấu hình được; circuit breaker; alert ngay |
| Chi phí scrape vượt dự toán do retry/duplicate | Trung bình | Trung bình | Cache 24h; dedupe trong batch trước khi gọi; ghi `scrape_cost_usd` mỗi product; daily budget cap (Phase 8) |
| Marketplace non-US trả nội dung tiếng bản địa | Cao | Thấp | Lưu `marketplace`; Phase 5 truyền locale vào prompt để rewrite đúng ngôn ngữ |
| Sản phẩm không tồn tại / đã gỡ | Cao | Thấp | Error code `not_found`, hiện rõ trong UI, không retry |

## Security Considerations

- Credential Oxylabs/Scrapingdog **chỉ** ở env. Không log Basic Auth header — cấu hình structlog redact key `authorization`, `password`, `api_key`.
- URL user nhập là input không tin cậy → **SSRF risk** ở bước resolve shortlink. Bắt buộc:
  - Chỉ follow redirect nếu host cuối thuộc `AMAZON_DOMAINS`
  - Chặn IP private/loopback/link-local (`10.*`, `192.168.*`, `127.*`, `169.254.*`, IPv6 ULA)
  - Max 3 hop, timeout 5s
- `raw_payload` chứa nội dung có bản quyền → không expose qua API public, chỉ dùng nội bộ.
- Giới hạn `max_links_per_batch` để chống abuse (mặc định 100).

## Next Steps

→ [Phase 3 — Image Pipeline & R2 Storage](./phase-03-image-pipeline.md)
→ [Phase 5 — AI Rewrite Engine](./phase-05-ai-rewrite-engine.md) (chạy song song được sau Phase 2)
