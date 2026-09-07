# Phase 4 — AI Rewrite bằng Gemini

**Priority:** P1 · **Effort:** 6h · **Status:** ✅ Done (2026-09-07)
**Context:** [plan.md](./plan.md) · [Phase 2](./phase-02-fetch-and-parse.md) · [Research 02 §2](./research/research-02-free-tier-options.md)

## Overview

Viết lại title + description bằng Gemini free tier. Yêu cầu gốc của user:
*"chỉnh sửa 1 chút xíu nhưng vẫn giữ đúng nội dung"* — ràng buộc **cứng**, không phải mong muốn.

## Key Insights

- **Một lời gọi cho cả title + description.** Free tier Gemini là **1.500 request/ngày**.
  Gộp 2 field vào 1 structured output → 1.500 sản phẩm/ngày free thay vì 750. Vừa tiết kiệm
  quota vừa nhanh gấp đôi.
- **LLM sẽ bịa số liệu** — không phải "có thể", mà là vài % chắc chắn. Đăng bán sai kích thước
  → khách trả hàng. Kiểm tra số liệu là ~40 dòng và chặn được hại thật → **giữ dù đơn giản hoá**.
- Nhưng **giữ bản đơn giản**: chỉ so tập số + brand. Không cần model-number regex, không cần
  similarity scoring, không cần escalate model. Nếu fail 2 lần → giữ bản gốc.
- **ĐỪNG dùng alias `gemini-flash-latest` — đã test và nó hỏng.**
  Alias trỏ model mới nhất, cũng chính là model đông người dùng nhất → trả
  **503 "This model is currently experiencing high demand"** liên tục, retry 3 lần vẫn 503.
  Đo 2026-09-07:

  | Model | Kết quả |
  |-------|---------|
  | `gemini-flash-latest` | **503** |
  | `gemini-3.8-flash` | **503** |
  | `gemini-3.7-flash` | OK, 1.9s, 349 tok ← **dùng cái này** |
  | `gemini-3.6-flash` | OK, 8.8s |
  | `gemini-3.5-flash` | OK, 5.5s |
  | `gemini-flash-lite-latest` | OK, 1.0s, 57 tok ← **fallback** |

  → Pin model ổn định trong env (`GEMINI_MODEL`), thêm `GEMINI_MODEL_FALLBACK`.
  Bắt 503 → thử fallback. Lưu ý `gemini-2.5-flash` có trong danh sách models nhưng
  gọi thì **404 "no longer available to new users"** — danh sách models không đáng tin
  tuyệt đối, phải test gọi thật.
- Free tier: **15 RPM** → phải throttle. Batch 20 link chạy tuần tự với delay là đủ.

## Requirements

- Một lời gọi Gemini trả cả `title` và `description` (structured output)
- **Không gọi LLM** khi `desc_source == "none"` — không cho bịa từ hư không
- Kiểm tra số liệu: số nào có trong gốc phải còn, số nào không có trong gốc không được thêm
- Fail → retry 1 lần với prompt nhắc lỗi → vẫn fail thì **giữ bản gốc**, đánh dấu `failed`
- Giữ đúng ngôn ngữ gốc theo marketplace
- Throttle 15 RPM

## Architecture

### Gọi Gemini

```python
from google import genai
from pydantic import BaseModel

class Rewritten(BaseModel):
    title: str
    description: str

client = genai.Client(api_key=settings.gemini_api_key)

resp = client.models.generate_content(
    model=settings.gemini_model,              # "gemini-flash-latest"
    contents=prompt,
    config={
        "response_mime_type": "application/json",
        "response_schema": Rewritten,          # structured output
        "temperature": 0.4,                    # thấp để giảm bịa, không 0 để vẫn có biến thể
    },
)
result = resp.parsed                           # -> Rewritten
```

SDK `google-genai` là sync → bọc `asyncio.to_thread`.

### Prompt

```
Bạn viết lại nội dung sản phẩm thương mại điện tử.

RÀNG BUỘC BẮT BUỘC — vi phạm là output không dùng được:
1. Giữ NGUYÊN mọi chi tiết sự kiện: con số, kích thước, đơn vị, mã model,
   tên thương hiệu, chất liệu, màu, số lượng, dung lượng, danh sách tương thích.
2. KHÔNG thêm bất kỳ thông tin nào không có trong bản gốc. Không bịa lợi ích,
   không bịa thông số, không dùng từ khẳng định ("tốt nhất", "số 1") nếu bản gốc không có.
3. KHÔNG bỏ bất kỳ chi tiết sự kiện nào có trong bản gốc.
4. Viết bằng ĐÚNG NGÔN NGỮ của bản gốc. Không dịch.
5. Chỉ đổi cách diễn đạt và cấu trúc câu — đây là paraphrase nhẹ, không phải viết lại.
6. Độ dài xấp xỉ bằng bản gốc (±20%).
7. Title ≤ 200 ký tự, giữ thương hiệu và mã model ở đầu.
8. Plain text. Không markdown, không HTML, không emoji.

NHỮNG CHI TIẾT PHẢI XUẤT HIỆN NGUYÊN VĂN TRONG OUTPUT:
{facts}

TITLE GỐC:
{title}

MÔ TẢ GỐC:
{description}
```

`{facts}` = danh sách số + đơn vị + brand trích sẵn bằng regex. Đưa vào prompt làm tăng
tỉ lệ giữ đúng rõ rệt — rẻ hơn nhiều so với chỉ dặn dò chung.

### Kiểm tra số liệu (bản đơn giản)

```python
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")

def norm_nums(text: str) -> set[str]:
    """Chuẩn hoá: '1,299.50' và '1299.5' là một."""
    out = set()
    for m in NUM_RE.findall(text):
        s = m.replace(",", "")
        try: out.add(f"{float(s):g}")
        except ValueError: pass
    return out

def check(orig: str, new: str, brand: str | None) -> list[str]:
    """Trả list lỗi. Rỗng = pass."""
    a, b = norm_nums(orig), norm_nums(new)
    errs = []
    if missing := a - b: errs.append(f"mất số: {sorted(missing)[:5]}")
    if added   := b - a: errs.append(f"bịa số: {sorted(added)[:5]}")     # nghiêm trọng nhất
    if brand and brand.lower() not in new.lower(): errs.append("mất brand")
    if new.strip() == orig.strip():  errs.append("không đổi gì")
    return errs
```

Cố tình đơn giản. **Không** dùng similarity scoring, **không** dùng model-number regex —
những thứ đó thuộc bản full-scale. `added` (bịa số) là lỗi nghiêm trọng nhất và cũng dễ bắt nhất.

Kiểm tra riêng cho title và description.

### Luồng xử lý

```python
async def rewrite(p: ProductData) -> RewriteOutcome:
    if p.desc_source == "none":
        return skipped("no_source_description")          # KHÔNG gọi LLM
    if len(p.description) < 40:
        return skipped("source_too_short")
    desc = p.description[:12000]                          # cắt ở ranh giới câu

    for attempt in (1, 2):
        r = await call_gemini(p, desc, hint=prev_errors if attempt == 2 else None)
        errs = check(p.title, r.title, p.brand) + check(desc, r.description, p.brand)
        if not errs:
            return ok(r)
        prev_errors = errs
    return failed(prev_errors)                            # GIỮ BẢN GỐC
```

**Không bao giờ** xuất bản output đã fail. Giữ bản gốc luôn an toàn hơn.

### Throttle

```python
# Free tier: 15 RPM. Giữ khoảng cách ≥4.5s giữa các lời gọi.
_last_call = 0.0
async def throttle():
    global _last_call
    wait = 4.5 - (time.monotonic() - _last_call)
    if wait > 0: await asyncio.sleep(wait)
    _last_call = time.monotonic()
```

Đếm request/ngày trong SQLite, hiển thị UI ("đã dùng 340/1500 hôm nay").
Gần trần → cảnh báo. Bắt lỗi 429 → chờ theo `retry-after`.

### ⚠️ Đánh đổi của free tier — ghi vào README

**Gemini free tier: Google có thể dùng prompt và response để cải thiện sản phẩm của họ.**
Mô tả sản phẩm Amazon vốn public nên phần lớn chấp nhận được. Không muốn →
đổi sang OpenAI `gpt-5-nano` (~$1/tháng ở 100 SP/ngày). Thiết kế đã tách `call_gemini()`
thành 1 hàm nên đổi provider là sửa 1 file.

## Related Code Files

**Create:**
- `app/services/rewrite.py`      — prompt, gọi Gemini, throttle, luồng retry
- `app/services/facts.py`        — trích số/đơn vị/brand, `check()`
- `tests/test_facts.py`
- `tests/test_rewrite.py`        — mock Gemini, test luồng retry + fallback

## Implementation Steps

1. **`facts.py`** — `norm_nums()`, trích đơn vị, `check()`. **Viết test trước** — sai ở đây là sai hết.
   Test: `"1,299.50"` vs `"1299.5"` → cùng tập; số bịa thêm → bắt được; mất số → bắt được.
2. **Prompt** trong `rewrite.py`, có `{facts}`.
3. **`call_gemini()`** — structured output với `response_schema`, `asyncio.to_thread`, xử lý 429.
4. **Throttle** + đếm quota ngày trong SQLite.
5. **Luồng retry** — attempt 2 kèm hint lỗi, fail → giữ bản gốc.
6. **Guard** `desc_source == "none"` / quá ngắn / cắt khi quá dài.
7. **CLI**: `uv run python -m app.cli rewrite <product_id>` in gốc / mới / lỗi cạnh nhau.
   Không tune được prompt nếu không có cái này.
8. **Chạy thử 20 sản phẩm thật**, xem tỉ lệ pass. Mục tiêu ≥85% pass ở attempt 1.

## Todo List

- [x] `norm_nums()` xử lý dấu phẩy ngăn cách + thập phân
- [x] Trích đơn vị + brand
- [x] `check()` trả list lỗi (mất số / bịa số / mất brand / không đổi)
- [x] Test `facts.py` ≥15 case gồm số định dạng châu Âu
- [x] Prompt với `{facts}` + ràng buộc ngôn ngữ
- [x] `call_gemini()` structured output + `asyncio.to_thread`
- [x] Throttle 4.5s + đếm quota ngày trong SQLite
- [x] Xử lý 429 theo `retry-after`
- [x] Guard: skip khi `desc_source=="none"` / <40 ký tự; cắt khi >12000
- [x] Retry 1 lần với hint lỗi → fail thì giữ bản gốc
- [x] **Không bao giờ** ghi đè `orig_title` / `orig_desc`
- [x] CLI `rewrite` hiển thị so sánh
- [x] Chạy thử 20 sản phẩm thật, đo tỉ lệ pass
- [x] Ghi cảnh báo "Google dùng data free tier" vào README

## Success Criteria

- [x] Chạy 20 sản phẩm thật: ≥85% `rewrite_status='ok'` ở attempt 1
- [x] **0 trường hợp** bịa số lọt vào output cuối
- [x] Sản phẩm `desc_source="none"` → `skipped`, **0 lời gọi Gemini** (assert)
- [x] Sản phẩm `amazon.de` → output tiếng Đức, không bị dịch sang tiếng Anh
- [x] Mock Gemini trả output thiếu số → `check()` bắt được → retry → giữ bản gốc
- [x] `orig_title` / `orig_desc` không bao giờ bị ghi đè
- [x] Throttle: 5 lời gọi liên tiếp mất ≥18s (chứng minh giữ dưới 15 RPM)
- [x] Counter quota ngày tăng đúng, reset sang ngày mới
- [x] Đổi `GEMINI_MODEL` sang tên sai → lỗi rõ ràng, app không crash

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| **LLM bịa thông số → bán sai sản phẩm** | Cao nếu không check | **Rất cao** | `check()` chặn cứng; bịa số = fail; giữ bản gốc khi fail |
| Hết quota 1.500/ngày | Thấp ở volume này | Trung bình | Counter + cảnh báo UI; gộp title+desc vào 1 lời gọi |
| Vượt 15 RPM → 429 | Trung bình | Thấp | Throttle 4.5s + backoff theo `retry-after` |
| Model chính trả 503 quá tải | **Cao** (đã gặp ngay lúc test) | Trung bình | Pin model ổn định thay vì alias `-latest`; `GEMINI_MODEL_FALLBACK`; retry 3 lần rồi mới chuyển |
| Model trong danh sách nhưng gọi 404 | Trung bình | Trung bình | Đã gặp với `gemini-2.5-flash`. Không tin danh sách models — `check_setup.py` gọi thật để verify |
| Prompt injection từ mô tả Amazon | Thấp | Trung bình | Nội dung đặt trong user content có delimiter; structured output giới hạn hình dạng; `check()` là lớp cuối |
| Free tier dùng data để train | **Chắc chắn** | Thấp–Trung bình | Ghi rõ trong README; đổi sang OpenAI nano nếu không chấp nhận (~$1/tháng) |
| `check()` quá nghiêm → pass rate thấp | Trung bình | Thấp | Chỉ check số + brand, không check similarity; đo trên 20 SP thật rồi tinh chỉnh |

## Security Considerations

- `gemini_api_key` chỉ ở env, không log.
- Mô tả Amazon là nội dung bên thứ 3 → có thể chứa prompt injection. Đặt trong phần content
  có delimiter rõ, không nhét vào system instruction. Structured output giới hạn hình dạng output.
- Không log full description (dữ liệu bản quyền + rác log). Chỉ log độ dài + danh sách lỗi.
- Giới hạn input 12.000 ký tự chống prompt bomb.

## Next Steps

→ [Phase 5 — Giao diện web & Jobs](./phase-05-web-ui-and-jobs.md)
