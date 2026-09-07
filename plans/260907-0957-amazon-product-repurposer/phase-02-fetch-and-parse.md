# Phase 2 — Lấy & Parse dữ liệu Amazon

**Priority:** P1 · **Effort:** 8h · **Status:** ✅ Done (2026-09-07)
**Context:** [plan.md](./plan.md) · [Phase 1](./phase-01-app-skeleton.md) · [Research 02 §1](./research/research-02-free-tier-options.md)

## Overview

Link Amazon → ASIN → HTML → `ProductData`. Đây là phase tốn công nhất của phương án free,
vì free nghĩa là **tự viết parser**.

## Key Insights

- **Chiến lược 2 tầng — nhưng đã đo lại, kỳ vọng phải hạ xuống.**
  ĐO 2026-09-07 từ IP thật: fetch trực tiếp **chỉ pass ~20-25%** (1/5 rồi 1/4 ở hai lần thử).
  Trang bị chặn trả **HTTP 200** kèm body **3.781 bytes** ("Click the button below to continue
  shopping" + marker `api-services-support@amazon.com`).
  **Đã thử và KHÔNG cứu được:** vào homepage lấy cookie trước rồi mới vào trang sản phẩm;
  xoay User-Agent; client mới hoàn toàn mỗi request; delay 6s.
  → Vẫn giữ tầng 1 vì fail rất nhanh (~1s) và khi pass thì tiết kiệm 1 credit,
  nhưng **đừng tính nó vào kế hoạch**.
- **Scrape.do chế độ THƯỜNG là đủ.** Đã test: `?token=..&url=..` không có `super=true`
  trả về HTML Amazon hợp lệ 2,4 MB. **Đừng bật `super=true`** — nó tốn 5-25x credit
  cho cùng kết quả.
- **Cache HTML là thứ bảo vệ quota.** Không có cache, mỗi lần thử lại template khác là
  đốt thêm 1 credit. Cache 24h theo `(marketplace, asin)`.
- **Tự rate-limit.** `request_delay_seconds=2.0` giữa các request. Hammer Amazon từ IP nhà
  là cách nhanh nhất để bị chặn cả IP — mất luôn tầng 1.
- **Phát hiện bị chặn phải chính xác.** Amazon trả **HTTP 200** kèm trang CAPTCHA, không phải 403.
  Check status code là không đủ.
- Description có **3 nguồn** độc lập, phải fallback theo chain — không có nguồn nào luôn tồn tại.

## Requirements

- Parse mọi biến thể URL Amazon phổ biến → `(asin, marketplace)`
- Fetch 2 tầng + cache + tự rate-limit
- Phát hiện trang CAPTCHA / bị chặn (không chỉ dựa status code)
- Parse HTML → `ProductData` với fallback chain cho description
- Test bằng fixture HTML đã lưu, không cần mạng

## Architecture

### URL Parser (`services/urls.py`)

```python
AMAZON_DOMAINS = {
    "amazon.com","amazon.co.uk","amazon.de","amazon.fr","amazon.it","amazon.es",
    "amazon.co.jp","amazon.ca","amazon.com.au","amazon.in","amazon.com.mx",
    "amazon.com.br","amazon.nl","amazon.se","amazon.pl","amazon.sg","amazon.ae",
}
SHORTENERS = {"amzn.to","amzn.eu","a.co","amzn.asia"}

PATH_PATTERNS = [
    r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})",
    r"/gp/aw/d/([A-Z0-9]{10})", r"/product/([A-Z0-9]{10})",
    r"/-/[a-z]{2}/dp/([A-Z0-9]{10})", r"/d/([A-Z0-9]{10})",
]

def parse(url: str) -> tuple[str, str]:   # (asin, marketplace) hoặc raise
    ...
```

Thứ tự: ASIN thô → resolve shortlink → domain → path patterns → query `?asin=` → lỗi rõ ràng.

**Bảo mật (SSRF):** resolve shortlink chỉ follow nếu host cuối thuộc `AMAZON_DOMAINS`,
max 3 hop, timeout 5s, chặn IP private.

### Fetcher 2 tầng (`services/fetcher.py`)

```python
BLOCK_MARKERS = [
    "api-services-support@amazon.com",       # trang "Sorry" của Amazon
    "Enter the characters you see below",     # CAPTCHA
    "To discuss automated access",
    "Type the characters you see in this image",
]

def looks_blocked(html: str) -> bool:
    if len(html) < 5000:                      # trang thật luôn lớn hơn nhiều
        return True
    if any(m in html for m in BLOCK_MARKERS):
        return True
    if 'id="productTitle"' not in html and "productTitle" not in html:
        return True                            # thiếu block chính = không phải trang SP
    return False

async def fetch(asin, marketplace, *, force=False) -> tuple[str, str]:
    """Trả (html, via). via: cache | direct | scrapedo"""
    # 1. cache (nếu chưa quá html_cache_hours)
    # 2. direct: httpx với headers trình duyệt thật + delay
    #    -> looks_blocked? sang bước 3
    # 3. scrapedo: GET https://api.scrape.do/?token=..&url=..&super=true&geoCode=us
    #    -> vẫn blocked -> raise FetchBlocked
    # lưu cache, trả về
```

Headers cho tầng direct — quan trọng, thiếu là bị chặn ngay:
```python
{
  "User-Agent": "<UA Chrome thật, cập nhật định kỳ>",
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9",
  "Accept-Encoding": "gzip, deflate, br",
  "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
  "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
  "Upgrade-Insecure-Requests": "1",
  "Cache-Control": "max-age=0",
}
```
Dùng `httpx.AsyncClient(follow_redirects=True, http2=True)`. HTTP/2 quan trọng —
client chỉ nói HTTP/1.1 là tín hiệu bot rõ ràng.

**Đếm quota Scrape.do:** mỗi lần dùng tầng 3, tăng counter trong SQLite theo tháng.
Hiển thị ở UI ("đã dùng 120/1000 tháng này"). Không có cái này thì hết quota mà không biết.

### Parser (`services/parser.py`)

Selector chính (Amazon layout desktop, tính tới 2026):

| Field | Selector | Ghi chú |
|-------|----------|---------|
| title | `#productTitle` | ổn định nhất |
| brand | **`tr.po-brand td.a-span9 span`** (bảng Product Overview) | ĐÃ TEST: `#bylineInfo` **không tồn tại** trên layout 2026. Fallback: `#productOverview_feature_div tr:nth-child(1) td:nth-child(2) span` |
| bullets | `#feature-bullets ul li span.a-list-item` | bỏ item chứa "See more" |
| description | `#productDescription` | có thể vắng |
| aplus | `#aplus`, `#aplus_feature_div` | strip HTML lấy text |
| price | `.a-price .a-offscreen` (lấy cái đầu) | |
| best seller | `#zeitgeistBadge_feature_div`, `.badge-wrapper`, text "Best Seller" | |
| free delivery | `#deliveryBlockMessage`, text "FREE delivery" | |
| ảnh | **xem bên dưới** | |

**Lấy ảnh — điểm mấu chốt.** Không parse `<img>` tag (chỉ có thumbnail). Amazon nhúng
JSON trong script:
```python
# Tìm trong <script>:  'colorImages': { 'initial': [ {...} ] }
# hoặc  data-a-dynamic-image  trên #landingImage
COLOR_IMAGES_RE = re.compile(r"'colorImages'\s*:\s*(\{.+?\}),\s*\n", re.S)
# fallback: attribute data-a-dynamic-image chứa JSON {url: [w,h], ...}
```
Từ JSON lấy `hiRes` (nếu có) hoặc `large`. Dedupe theo image ID.
Nếu cả hai fail → parse `#altImages img` rồi nâng hi-res ở Phase 3.

**Fallback chain cho description:**
```
1. #productDescription (text ≥ 50 ký tự)   -> desc_source="product_description"
2. ghép bullets thành đoạn văn              -> desc_source="bullets"
3. text từ #aplus (≥ 50 ký tự)              -> desc_source="aplus"
4. rỗng                                      -> desc_source="none"
```
`desc_source="none"` → Phase 4 **skip rewrite**, không cho AI bịa.

### `ProductData`

```python
class ProductData(BaseModel):
    asin: str
    marketplace: str
    title: str
    description: str
    desc_source: Literal["product_description","bullets","aplus","none"]
    bullets: list[str] = []
    images: list[str] = []          # URL, đã dedupe, đã sắp thứ tự
    brand: str | None = None
    price: str | None = None
    is_best_seller: bool = False
    has_free_delivery: bool = False
```

## Related Code Files

**Create:**
- `app/services/urls.py`
- `app/services/fetcher.py`
- `app/services/parser.py`
- `app/services/quota.py`          — đếm quota Scrape.do theo tháng
- `data/fixtures/*.html`           — ≥5 trang Amazon đã lưu
- `tests/test_urls.py`
- `tests/test_parser.py`
- `tests/test_blocked_detection.py`

## Implementation Steps

1. **`urls.py`** + test ≥25 case URL. Viết test trước — đây là nơi bug ẩn nhiều nhất.
2. **Lưu fixture HTML.** Mở trình duyệt, "Save page as" → `data/fixtures/`. Cần:
   - sản phẩm thường có `#productDescription`
   - sản phẩm **không có** `#productDescription` (chỉ bullets)
   - sản phẩm có A+ content
   - sản phẩm Best Seller (có badge)
   - sản phẩm 1 ảnh
   - **một trang CAPTCHA** (để test `looks_blocked`)
   - một trang `amazon.de` (Unicode)
3. **`looks_blocked()`** + test với fixture CAPTCHA và fixture bình thường.
4. **`parser.py`** — viết từng field một, test ngay bằng fixture. Mỗi selector có fallback:
   selector chính fail → thử selector phụ → trả `None` (không raise).
5. **Lấy ảnh từ JSON nhúng** — phần khó nhất của parser. Test kỹ với fixture nhiều ảnh.
6. **Fallback chain description** + test với fixture thiếu `#productDescription`.
7. **`fetcher.py`** tầng direct — headers đầy đủ, HTTP/2, delay, timeout 20s.
8. **Tầng Scrape.do** + `quota.py` đếm theo tháng.
9. **Cache** đọc/ghi `html_cache`, TTL từ config.
10. **CLI**: `uv run python -m app.cli fetch <url>` in `ProductData` ra JSON.
    Không debug parser được nếu không có cái này.

## Todo List

- [x] `urls.py` + test ≥25 case (shortlink, locale prefix, mobile, query rác, ASIN thô)
- [x] SSRF guard cho resolve shortlink (allowlist domain, chặn IP private, max 3 hop)
- [x] Lưu ≥7 fixture HTML gồm **1 trang CAPTCHA**
- [x] `looks_blocked()` + test cả 2 chiều
- [x] Parser: title, brand, bullets, description, aplus, price
- [x] Parser: badge best seller + free delivery
- [x] Parser ảnh từ JSON nhúng (`colorImages` / `data-a-dynamic-image`) + fallback `#altImages`
- [x] Dedupe ảnh theo image ID
- [x] Fallback chain description 4 bước
- [x] Fetcher tầng direct: headers đầy đủ + HTTP/2 + delay + timeout
- [x] Fetcher tầng Scrape.do
- [x] `quota.py` đếm Scrape.do theo tháng
- [x] Cache HTML 24h theo `(marketplace, asin)`
- [x] CLI `fetch <url>` in ProductData
- [x] Test parser chạy được **không cần mạng** (chỉ fixture)

## Success Criteria

- [x] `uv run python -m app.cli fetch <link thật>` trả đủ title + ảnh + description
- [x] Test parser chạy offline, pass toàn bộ fixture
- [x] Fixture không có `#productDescription` → `desc_source == "bullets"`, description không rỗng
- [x] Fixture CAPTCHA → `looks_blocked() == True`
- [x] Fixture Best Seller → `is_best_seller == True`; fixture thường → `False`
- [x] Fetch lần 2 cùng ASIN trong 24h → `via == "cache"`, **0 HTTP request** (assert bằng `respx`)
- [x] Mock tầng direct trả trang CAPTCHA → tự động chuyển Scrape.do (assert)
- [x] Quota counter tăng đúng khi dùng Scrape.do, không tăng khi dùng direct/cache
- [x] Số ảnh parse được ≥ số ảnh nhìn thấy trên trang (không sót)

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| **Amazon đổi layout → parser vỡ** | **Cao** (đây là cái giá của free) | Cao | Test fixture chạy trong CI; mỗi selector có fallback; parser trả `None` thay vì raise → mất 1 field chứ không mất cả sản phẩm |
| Bị chặn IP nhà do fetch quá nhanh | **Cao** nếu không delay | Trung bình | `request_delay_seconds=2.0`; tuần tự, không song song ở tầng direct |
| **Hết quota Scrape.do giữa tháng** | **Cao** (đã đo: ~75-80% request phải dùng nó) | **Cao** | Counter + cảnh báo UI từ 80%; cache 24h là phòng tuyến chính; vẫn thử tầng direct trước |
| Vô tình bật `super=true` → cháy quota 5-25x | Trung bình | **Cao** | ĐÃ TEST chế độ thường là đủ. Hardcode không truyền `super`; ghi comment rõ trong code |
| Không lấy được ảnh hi-res (JSON đổi format) | Trung bình | Cao | 3 tầng fallback: `colorImages` → `data-a-dynamic-image` → `#altImages` + nâng hi-res ở Phase 3 |
| Parse sai ASIN (regex fallback) | Thấp | Trung bình | Chỉ dùng path pattern + query param, **bỏ** regex quét toàn URL (dễ false positive) |
| `desc_source="none"` tỉ lệ cao | Trung bình | Trung bình | Hiện rõ ở UI; nếu >30% thì parser có vấn đề, không phải sản phẩm |

## Security Considerations

- **SSRF** ở resolve shortlink: allowlist `AMAZON_DOMAINS`, chặn IP private/loopback/link-local, max 3 hop, timeout 5s.
- Token Scrape.do và mọi key chỉ ở env, không log.
- HTML cache có thể lớn → giới hạn kích thước lưu (cắt ở 2MB), dọn bản ghi cũ hơn 7 ngày.
- HTML từ Amazon là nội dung không tin cậy — khi render ra UI phải escape (Jinja2 autoescape bật mặc định, **đừng** dùng `|safe`).

## Next Steps

→ [Phase 3 — Pipeline ảnh & Sticker](./phase-03-image-pipeline.md)
