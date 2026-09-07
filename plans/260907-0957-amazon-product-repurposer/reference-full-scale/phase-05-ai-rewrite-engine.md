# Phase 5 — AI Rewrite Engine

**Priority:** P1 · **Effort:** 14h · **Status:** Pending
**Context:** [plan.md](./plan.md) · [Phase 2](./phase-02-ingestion-scraper-adapter.md) · [Research 01 §4](./research/research-01-providers-and-costs.md)

## Overview

Viết lại title + description bằng OpenAI. Yêu cầu gốc của user: *"viết lại bằng cách chỉnh sửa 1 chút xíu nhưng vẫn giữ đúng nội dung"* — đây là ràng buộc **cứng**, không phải mong muốn.

## Key Insights

- **LLM sẽ bịa số liệu.** Đây không phải "có thể", mà là "chắc chắn sẽ xảy ra ở tỉ lệ vài %". Ở 2000 sản phẩm/ngày, vài % = hàng chục sản phẩm sai thông số mỗi ngày. Đăng bán sản phẩm sai kích thước/công suất/tương thích → khách trả hàng, mất tiền, mất uy tín.
  → **Fidelity Validator là bắt buộc, không phải optional.**
- Ràng buộc "chỉnh sửa 1 chút xíu" là con dao hai lưỡi: quá giống bản gốc thì vô nghĩa (không tránh được duplicate content), quá khác thì lệch nội dung. Cần **đo** cả hai hướng.
- **`gpt-5-mini` có lịch shutdown 11/12/2026.** Model name phải ở env + có fallback chain. Hardcode = app chết vào tháng 12.
- Description Amazon có thể chứa HTML. Phải strip trước khi đưa vào prompt (tiết kiệm token + tránh LLM sinh HTML rác).
- Marketplace non-US → nội dung tiếng Đức/Nhật/Pháp. Prompt phải giữ đúng ngôn ngữ gốc, trừ khi user chọn dịch.

## Requirements

### Functional
- Rewrite title (ngắn, ≤200 ký tự) và description (dài) độc lập, model cấu hình riêng
- Structured Outputs (JSON schema, strict mode) — không parse text tự do
- **Fidelity Validator**: so số liệu, đơn vị, model number, brand giữa gốc và bản mới
- Similarity guard: quá giống (≥0.95) hoặc quá khác (<0.60) → retry
- Retry tối đa 2 lần; vẫn fail → giữ bản gốc, đánh dấu `rewrite_status='failed'`
- **Không rewrite khi `description_source == "none"`** — không cho LLM bịa từ hư không
- Giữ đúng ngôn ngữ gốc theo marketplace
- Ghi `llm_cost_usd` mỗi sản phẩm
- Tone cấu hình được ở batch level: `neutral` | `marketing` | `concise`

### Non-functional
- p95 ≤ 12s cho cả title + description
- Chi phí ≤ $0.002/sản phẩm ở cấu hình mặc định
- Không bao giờ ghi đè `orig_title`/`orig_description` — luôn giữ bản gốc

## Architecture

### Model config & fallback

```python
# services/rewrite/models.py
MODEL_CHAIN = {
    "title":       [settings.openai_model_title,       settings.openai_model_fallback],
    "description": [settings.openai_model_description, settings.openai_model_fallback],
}
# Giá để tính cost — cập nhật khi OpenAI đổi bảng giá
PRICING_USD_PER_1M = {
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-5-nano": {"input": 0.05, "output": 0.40},
}
```
Model không có trong `PRICING_USD_PER_1M` → log warning, cost ghi 0, **không** fail job.

Lỗi `model_not_found` / `model_deprecated` → tự động thử model tiếp theo trong chain và log ở mức ERROR (dấu hiệu cần cập nhật env).

### Prompt design

**System prompt (description):**
```
You rewrite e-commerce product descriptions.

HARD CONSTRAINTS — violating any of these makes the output unusable:
1. Preserve EVERY factual detail: numbers, measurements, units, model numbers,
   part numbers, brand names, material names, colors, quantities, capacities,
   compatibility lists, certifications, warranty terms.
2. Do NOT add any fact not present in the source. No invented benefits,
   no invented specifications, no superlatives that imply a claim
   ("best", "#1", "award-winning") unless present in the source.
3. Do NOT remove any factual detail present in the source.
4. Write in the SAME LANGUAGE as the source. Do not translate.
5. Change wording and sentence structure only — this is a light paraphrase,
   not a rewrite. Aim for roughly 70-85% word-level difference in phrasing
   while keeping identical meaning and identical facts.
6. Keep approximately the same length (±20%).
7. Output plain text. No markdown, no HTML, no emoji.

Tone: {tone}
```

**User message:**
```
SOURCE DESCRIPTION:
{description}

FACTS THAT MUST APPEAR VERBATIM IN YOUR OUTPUT:
{extracted_facts}
```

`extracted_facts` — trích trước bằng regex, đưa vào prompt để tăng tỉ lệ giữ đúng. Đây là kỹ thuật rẻ và hiệu quả hơn nhiều so với chỉ dặn dò chung chung.

**Title prompt** tương tự, thêm ràng buộc: giữ brand + model number ở đầu, ≤200 ký tự (giới hạn Amazon), không nhồi keyword.

### Structured Outputs

```python
class RewriteResult(BaseModel):
    text: str
    preserved_facts: list[str]      # LLM tự liệt kê fact nó đã giữ — dùng để cross-check
    changed_nothing: bool           # LLM tự báo nếu không thể paraphrase (VD text quá ngắn)

# gọi qua client.chat.completions.parse(..., response_format=RewriteResult)
# temperature=0.4  (thấp để giảm bịa, không 0 để vẫn có biến thể ngôn từ)
```

`preserved_facts` là self-report của LLM — **không tin tuyệt đối**, nhưng dùng làm tín hiệu phụ khi validator biên.

### Fidelity Validator — phần quan trọng nhất

```python
# services/rewrite/validator.py

NUMBER_RE   = re.compile(r"\b\d+(?:[.,]\d+)?\b")
UNIT_RE     = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*"
    r"(mm|cm|m|in|inch|inches|ft|kg|g|lb|lbs|oz|ml|l|liter|litre|"
    r"w|kw|v|mah|ah|hz|ghz|mhz|gb|tb|mb|kb|bit|mp|fps|rpm|psi|"
    r"°c|°f|%|pcs|pack|count|ct)\b", re.I)
MODEL_RE    = re.compile(r"\b(?=[A-Z0-9\-]{4,20}\b)(?=.*\d)[A-Z0-9][A-Z0-9\-]{3,19}\b")

@dataclass
class FidelityReport:
    passed: bool
    missing_numbers: list[str]      # có trong gốc, mất trong bản mới  -> NGHIÊM TRỌNG
    added_numbers: list[str]        # không có trong gốc, xuất hiện mới -> NGHIÊM TRỌNG (bịa)
    missing_units: list[str]
    missing_models: list[str]
    missing_brand: bool
    similarity: float               # 0..1
    length_ratio: float
    severity: Literal["ok", "warn", "fail"]
```

Luật quyết định:

| Điều kiện | Kết quả |
|-----------|---------|
| `added_numbers` không rỗng | **fail** — LLM bịa số. Nghiêm trọng nhất. |
| `missing_numbers` không rỗng | **fail** — mất thông số |
| `missing_models` không rỗng | **fail** |
| `missing_brand` (brand có trong gốc) | **fail** |
| `similarity ≥ 0.95` | **fail** — không thay đổi gì, vô nghĩa |
| `similarity < 0.60` | **fail** — lệch quá xa bản gốc |
| `length_ratio` ngoài `[0.7, 1.35]` | **warn** |
| `missing_units` không rỗng | **warn** |
| còn lại | **ok** |

Chuẩn hoá trước khi so:
- Số: `1,299.50` và `1299.5` là một → strip separator, parse float, so với `math.isclose`
- Locale: dấu phẩy thập phân châu Âu (`1.299,50`) → detect theo marketplace
- Đơn vị: `inch`/`inches`/`in`/`"` → chuẩn hoá về canonical form
- Case-insensitive cho brand và unit

`similarity` = `difflib.SequenceMatcher` trên token đã normalize (lowercase, bỏ dấu câu). Rẻ, đủ dùng. Không cần embedding.

**Xử lý fail:**
```
attempt 1 fail → retry với prompt bổ sung:
    "Your previous attempt {removed X / invented Y}. Fix this. The following
     must appear exactly: {missing_facts}"
attempt 2 fail → dùng model mạnh hơn trong chain (nano → mini)
attempt 3 fail → GIỮ BẢN GỐC, rewrite_status='failed', rewrite_flags=<report>
```

**Không bao giờ** xuất bản output đã fail validator. Giữ bản gốc luôn an toàn hơn.

### Guard trước khi gọi LLM

```python
if product.description_source == "none":
    → rewrite_status='skipped', flag='no_source_description'
    → KHÔNG gọi LLM (không cho bịa)
if len(description) < 40:
    → rewrite_status='skipped', flag='source_too_short'
if len(description) > 12000:
    → truncate ở ranh giới câu, flag='source_truncated'
```

### Cost tracking

```python
usage = response.usage
cost = (usage.prompt_tokens / 1e6) * price["input"] \
     + (usage.completion_tokens / 1e6) * price["output"]
# cộng dồn cả retry vào product.llm_cost_usd
```

Retry **có tính phí**. Phải cộng dồn, nếu không cost report sai.

### Batching & rate limit

- OpenAI có rate limit theo TPM/RPM. Ở 2000 SP/ngày (~1.4/phút) không đụng trần, nhưng burst batch 100 link cùng lúc thì có.
- Dùng `asyncio.Semaphore(10)` cho lời gọi OpenAI ở worker level.
- Bắt `RateLimitError` → backoff theo header `retry-after`.
- **Không** dùng OpenAI Batch API ở MVP: rẻ hơn 50% nhưng độ trễ tới 24h, phá UX. Ghi nhận cho tương lai nếu có luồng "xử lý qua đêm".

## Related Code Files

**Create:**
- `apps/api/src/app/services/rewrite/__init__.py`
- `apps/api/src/app/services/rewrite/models.py`        — model chain + pricing
- `apps/api/src/app/services/rewrite/prompts.py`       — system/user prompt templates
- `apps/api/src/app/services/rewrite/client.py`        — OpenAI wrapper + retry + cost
- `apps/api/src/app/services/rewrite/validator.py`     — Fidelity Validator
- `apps/api/src/app/services/rewrite/facts.py`         — trích số/đơn vị/model number
- `apps/api/src/app/services/rewrite/service.py`       — orchestrate: guard → call → validate → retry
- `apps/api/src/app/services/rewrite/html.py`          — strip HTML từ description
- `apps/api/tests/test_facts_extraction.py`
- `apps/api/tests/test_fidelity_validator.py`
- `apps/api/tests/test_rewrite_service.py`
- `apps/api/tests/fixtures/rewrite/`                   — cặp (gốc, viết lại) mẫu: pass/fail

## Implementation Steps

1. **`html.py`** — strip HTML an toàn (dùng `selectolax` hoặc `bleach`), giữ line break, gộp whitespace.
2. **`facts.py`** — trích số, đơn vị, model number, brand. **Viết test trước.** Đây là nền của validator, sai ở đây là sai hết.
   - Test với text tiếng Anh, Đức (dấu phẩy thập phân), Nhật (số full-width).
3. **`validator.py`** — implement bảng luật trên. Test với ≥20 cặp (gốc, viết lại) thủ công gồm cả case bịa số.
4. **`prompts.py`** — template có `{tone}`, `{extracted_facts}`, `{language_hint}`.
5. **`client.py`** — OpenAI async client dùng chung, Structured Outputs, đo token, tính cost, xử lý `RateLimitError` + `model_not_found` → fallback chain.
6. **`service.py`** — orchestrate đầy đủ: guard → gọi → validate → retry (có prompt bổ sung) → escalate model → fallback giữ bản gốc.
7. **CLI**: `python -m app.cli rewrite <product_id>` in ra gốc / mới / `FidelityReport` cạnh nhau. Không thể tune prompt mà không có cái này.
8. **Eval set nhỏ** — 30 sản phẩm thật đa dạng ngành hàng, chạy batch, xem tỉ lệ pass. Mục tiêu ≥90% pass ở attempt 1.

## Todo List

- [ ] `html.py` strip HTML giữ line break
- [ ] `facts.py`: `extract_numbers`, `extract_units`, `extract_models`, normalize theo locale
- [ ] Test `facts.py` với EN / DE / JP
- [ ] `validator.py` + `FidelityReport` + bảng luật severity
- [ ] Similarity qua `SequenceMatcher` trên token normalized
- [ ] Test validator ≥20 cặp gồm case bịa số, mất số, quá giống, quá khác
- [ ] `prompts.py` với tone + extracted_facts + language hint
- [ ] `client.py`: Structured Outputs, cost tracking, rate-limit backoff
- [ ] Model fallback chain + xử lý `model_not_found`
- [ ] Guard: skip khi `description_source=="none"` / quá ngắn / truncate khi quá dài
- [ ] `service.py`: retry có prompt bổ sung → escalate model → giữ bản gốc
- [ ] Cộng dồn cost qua mọi lần retry
- [ ] `asyncio.Semaphore` giới hạn concurrency OpenAI
- [ ] CLI `rewrite <product_id>` hiển thị so sánh
- [ ] Eval set 30 sản phẩm, đo pass rate

## Success Criteria

- [ ] Chạy eval 30 sản phẩm: ≥90% `rewrite_status='ok'` ở attempt 1
- [ ] **0 trường hợp** `added_numbers` lọt qua validator vào output cuối
- [ ] Sản phẩm không có description → `rewrite_status='skipped'`, **0 lời gọi OpenAI** (assert)
- [ ] Sản phẩm marketplace `amazon.de` → output tiếng Đức, không bị dịch sang tiếng Anh
- [ ] Cố tình mock LLM trả output thiếu số → validator `fail`, service retry, cuối cùng giữ bản gốc
- [ ] `orig_title` / `orig_description` **không bao giờ** bị ghi đè
- [ ] Cost trung bình/sản phẩm ≤ $0.002, có ghi vào `llm_cost_usd` gồm cả retry
- [ ] Set `OPENAI_MODEL_DESCRIPTION` sang tên model không tồn tại → fallback chain hoạt động, job không chết
- [ ] Similarity nằm trong `[0.60, 0.95)` cho ≥90% output pass

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| **LLM bịa thông số → sản phẩm bán sai** | **Cao** nếu không validate | **Rất cao** | Fidelity Validator chặn cứng; `added_numbers` = fail tuyệt đối; giữ bản gốc khi fail |
| `gpt-5-mini` shutdown 11/12/2026 giữa production | **Chắc chắn** | Cao | Model qua env; fallback chain; alert khi dùng fallback; ghi lịch review tháng 11/2026 |
| Chi phí OpenAI vượt dự toán do retry | Trung bình | Trung bình | Cost tracking per product; daily budget cap (Phase 8); giới hạn 2 retry |
| Validator quá nghiêm → pass rate thấp, tốn retry | Trung bình | Trung bình | Tune ngưỡng similarity trên eval set thật; `missing_units` là warn không phải fail |
| Validator quá lỏng → lọt lỗi | Trung bình | Cao | `added_numbers`/`missing_numbers`/`missing_models` luôn là fail cứng, không tune |
| Rewrite không xoá được vi phạm bản quyền | **Chắc chắn** | Cao | Đây là vấn đề pháp lý, không phải kỹ thuật — xem `source_authorization` ở plan.md |
| OpenAI rate limit khi batch lớn | Trung bình | Thấp | Semaphore + backoff theo `retry-after` |
| Model number regex false positive (bắt nhầm từ thường) | Trung bình | Thấp | Regex yêu cầu có ít nhất 1 chữ số; kiểm tra trên eval set |

## Security Considerations

- **Prompt injection từ description Amazon.** Description là nội dung bên thứ 3 — có thể chứa `"Ignore previous instructions..."`. Giảm thiểu:
  - Đặt nội dung sản phẩm trong user message, **không** trong system message
  - Bọc trong delimiter rõ ràng, dặn model coi là dữ liệu
  - Structured Outputs giới hạn hình dạng output → injection khó gây hại thực
  - Validator là lớp phòng thủ cuối: output lệch bất thường sẽ fail similarity
- `openai_api_key` chỉ ở env, redact trong log.
- Không log full description ra stdout (dữ liệu bản quyền + tốn log storage). Chỉ log hash + độ dài + `FidelityReport`.
- Giới hạn độ dài input (12000 ký tự) chống prompt bomb làm tăng chi phí.

## Next Steps

→ [Phase 6 — Job Orchestration & REST API](./phase-06-job-orchestration-api.md)
