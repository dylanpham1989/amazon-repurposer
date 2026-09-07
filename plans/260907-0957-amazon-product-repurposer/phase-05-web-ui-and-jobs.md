# Phase 5 — Giao diện web & Jobs

**Priority:** P1 · **Effort:** 8h · **Status:** ✅ Done (2026-09-07)
**Context:** [plan.md](./plan.md) · Phase [2](./phase-02-fetch-and-parse.md) · [3](./phase-03-image-pipeline.md) · [4](./phase-04-ai-rewrite.md)

## Overview

Ghép mọi thứ lại. **Luồng do user chốt (2026-09-07):**

```
/login  ──password──►  /  (giao diện chính)
                        │
                        ├─ ô nhập link Amazon  +  nút Submit
                        │
                        └─► KẾT QUẢ hiện ngay dưới form, gồm:
                              • Title cũ        / Title mới           [copy] [copy]
                              • Description cũ  / Description mới     [copy] [copy]
                              • Ảnh cũ (đủ số lượng, có bao nhiêu lấy bấy nhiêu)
                                ↕ tương ứng 1-1 theo vị trí
                              • Link ảnh mới + thumbnail               [copy] mỗi ảnh
```

**MỌI ô hiển thị đều có nút copy ở cuối.** Yêu cầu tường minh của user, không phải
nice-to-have — công cụ này tồn tại để copy nội dung ra chỗ khác.

FastAPI + Jinja2 + HTMX — không build step, không `node_modules`.

## Key Insights

- **Kết quả hiện ngay trên trang chính, không bắt điều hướng.** Dán link → submit →
  cuộn xuống thấy kết quả. Không có bước "vào danh sách batch rồi click từng sản phẩm".
  Trang lịch sử vẫn có nhưng là phụ.
- **HTMX polling thay SSE.** `hx-trigger="every 2s"` là **một attribute** thay cho
  EventSource + backoff + polling fallback. App 1 người dùng, polling 2s hoàn toàn ổn.
- **Job nền = `asyncio.create_task` + cột status trong SQLite.** Không Redis, không ARQ.
  App restart giữa chừng → lúc startup quét `status='running'` → set `failed`, user bấm retry.
- **Ảnh cũ và ảnh mới phải ghép cặp 1-1 theo `position`.** User cần đối chiếu "ảnh thứ 3
  gốc → ảnh thứ 3 đã dán sticker". Xếp lệch là vô dụng.
- **`navigator.clipboard` chỉ chạy trên HTTPS hoặc `localhost`.** Mở qua IP LAN sẽ mất
  toàn bộ nút copy — tức mất tính năng chính. Phải biết trước.
- **Xử lý tuần tự, không song song.** Fetch song song = bị Amazon chặn IP. Delay 2s là cố ý.

## Requirements

- `/login`: 1 ô mật khẩu (khớp `APP_PASSWORD`), sai thì báo lỗi rõ
- `/`: ô nhập link (textarea, mỗi dòng 1 link) + nút Submit
- Kết quả hiện ngay dưới form, cập nhật realtime khi đang chạy
- Mỗi sản phẩm hiện: title cũ/mới, description cũ/mới, ảnh cũ (đủ số lượng), ảnh mới + thumbnail
- **Nút copy ở cuối MỌI ô**: title cũ, title mới, desc cũ, desc mới, từng URL ảnh mới
- Thêm nút "Copy tất cả link ảnh mới" (1 URL/dòng) — thao tác dùng nhiều nhất
- Sửa tay title/description; rewrite lại 1 sản phẩm
- Template editor có preview live; upload sticker
- Export JSON / CSV / ZIP; xoá batch (xoá cả object R2)
- Hiển thị quota: Scrape.do (tháng), Gemini (ngày), R2 (GB)

## Architecture

### Job runner (`app/services/jobs.py`)

```python
async def run_batch(batch_id: str):
    """Chạy trong asyncio.create_task. Tuần tự, không song song."""
    for product in await get_pending(batch_id):
        try:
            await set_stage(product.id, "fetch")
            data = await fetcher.fetch_and_parse(product.source_url)
            await save_product_data(product.id, data)

            await set_stage(product.id, "images")
            await images.process(product.id, data, template)

            await set_stage(product.id, "rewrite")
            await rewrite.run(product.id, data)

            await mark_done(product.id)
        except Exception as e:
            await mark_failed(product.id, str(e))
        await bump_batch_counters(batch_id)
        await asyncio.sleep(settings.request_delay_seconds)
```

Ảnh và rewrite chạy **tuần tự** chứ không `gather` — cả hai đều đụng rate limit
(Amazon CDN và Gemini 15 RPM). Song song không nhanh hơn bao nhiêu mà tăng rủi ro bị chặn.

**Khôi phục sau restart:** lúc startup `UPDATE products SET status='failed',
error='app restarted' WHERE status='running'`. User bấm retry.

### Trang

| Route | Nội dung |
|-------|----------|
| `GET/POST /login` | 1 ô mật khẩu |
| `GET /` | **ô nhập link + submit + kết quả batch gần nhất** |
| `POST /submit` | tạo batch → `create_task(run_batch)` → redirect `/?batch={id}` |
| `GET /results/{batch_id}` | **partial HTML** — HTMX poll swap vào `/` |
| `POST /products/{id}` | lưu sửa tay |
| `POST /products/{id}/rewrite` | rewrite lại 1 sản phẩm |
| `GET /batches` | lịch sử (phụ) |
| `GET /templates` · `GET /templates/{id}` | danh sách + editor |
| `POST /templates/preview` | trả `image/webp` |
| `POST /assets` | upload sticker PNG |
| `GET /batches/{id}/export?fmt=json|csv|zip` | export |
| `DELETE /batches/{id}` | xoá + xoá object R2 |

### Bố cục khối kết quả 1 sản phẩm (`_product.html`)

```
+---------------------------------------------------------------+
| ASIN B0863TXGM3 . [done] . nguon mo ta: product_description    |
| lay qua: scrapedo . [!] rewrite failed (neu co) . [Rewrite lai]|
+---------------------------------------------------------------+
| TITLE                                                          |
|  cu  | Sony WH-1000XM4 Wireless Noise Cancelling...    [copy]  |
|  moi | Sony WH-1000XM4 Wireless Headphones with...     [copy][sua] |
+---------------------------------------------------------------+
| DESCRIPTION                                                    |
|  cu  | Sony's intelligent industry-leading...          [copy]  |
|  moi | Sony's smart, category-leading noise...         [copy][sua] |
+---------------------------------------------------------------+
| ANH  (26 anh)                  [copy TAT CA link anh moi]      |
| +----------------------+-------------------------------------+ |
| | #1  anh goc [thumb]  | anh moi [thumb]                     | |
| |                      | https://cdn.../00-a1b2.webp  [copy] | |
| +----------------------+-------------------------------------+ |
| | #2  [thumb]          | [thumb] https://cdn/01-c3d4  [copy] | |
| +----------------------+-------------------------------------+ |
|                    ... du 26 hang ...                          |
+---------------------------------------------------------------+
```

Ghép cặp theo `position` — ảnh gốc `#N` **cùng hàng** với ảnh mới `#N`.
Ảnh nào xử lý lỗi → cột phải hiện "lỗi: ..." thay cho thumbnail,
**không** được đẩy lệch các hàng còn lại.

### Nút copy — macro Jinja dùng lại

```html
{% macro copyable(text, label="", multiline=False) %}
<div class="flex items-start gap-2">
  <div class="flex-1 {{ 'whitespace-pre-wrap' if multiline }}">{{ text }}</div>
  <button type="button" class="copy-btn shrink-0"
          data-copy="{{ text }}" title="Copy {{ label }}">copy</button>
</div>
{% endmacro %}
```

Toàn bộ JS của app — 10 dòng. Dùng **event delegation** nên hoạt động cả với
nội dung HTMX vừa swap vào (nếu gắn listener trực tiếp lên nút thì nội dung mới sẽ mất copy):

```js
document.addEventListener('click', async (e) => {
  const b = e.target.closest('.copy-btn'); if (!b) return;
  const t = b.dataset.copy;
  try { await navigator.clipboard.writeText(t); }
  catch { const a = document.createElement('textarea');   // fallback ngữ cảnh không an toàn
          a.value = t; document.body.appendChild(a); a.select();
          document.execCommand('copy'); a.remove(); }
  const o = b.textContent; b.textContent = 'OK';
  setTimeout(() => b.textContent = o, 1200);
});
```

Nút "Copy tất cả link ảnh mới" = `"\n".join(cdn_urls)` trong `data-copy`.

### HTMX polling

```html
<!-- Chi poll khi batch dang chay. Xong thi server tra partial KHONG co hx-trigger
     -> polling tu dung. Khong can JS. -->
<div id="results" hx-get="/results/{{ batch.id }}"
     hx-trigger="every 2s" hx-swap="outerHTML">
  {% include "_results.html" %}
</div>
```

### Export

- **JSON**: `orig_*`, `new_*`, list CDN URL, cờ lỗi
- **CSV**: 1 dòng/sản phẩm, cột `image_1..image_N`
- **ZIP**: tải ảnh từ R2 → zip. Volume nhỏ (≤20 SP/batch) nên buffer RAM chấp nhận được.

### Trang quota

Ba con số — thiếu là hết quota mà không biết:
Scrape.do `X/1000` tháng · Gemini `X/1500` ngày · R2 `X.X GB/10 GB`

### Template editor

Lưới 3×3 chọn anchor (khớp trực tiếp 9 giá trị `ANCHORS`) + slider scale/offset/opacity/rotation.
Preview: HTMX `hx-trigger="change delay:500ms"` → POST `/templates/preview` → swap `<img>`.
Không cần JS tự viết.

### Export

- **JSON**: đầy đủ, gồm `orig_*`, `new_*`, danh sách CDN URL, cờ lỗi
- **CSV**: 1 dòng/sản phẩm, cột `image_1..image_N`
- **ZIP**: tải ảnh từ R2 → zip. Volume nhỏ (≤20 SP/batch) nên buffer trong RAM là chấp nhận được.
  Vượt 50 sản phẩm thì chuyển streaming.

### Trang quota

Hiển thị 3 con số — thiếu là hết quota mà không biết:
- Scrape.do: `X / 1000` tháng này
- Gemini: `X / 1500` hôm nay
- R2: `X.X GB / 10 GB`

## Related Code Files

**Create:**
- `app/services/jobs.py`
- `app/services/export.py`
- `app/routes/batches.py`, `app/routes/products.py`, `app/routes/templates.py`
- `app/templates/`: `index.html`, `batch.html`, `_rows.html`, `product.html`,
  `templates_list.html`, `template_edit.html`, `quota.html`
- `tests/test_jobs.py`, `tests/test_export.py`

## Implementation Steps

1. **`jobs.py::run_batch`** tuần tự + cập nhật `stage` từng bước + counter.
2. **Khôi phục sau restart** ở lifespan startup.
3. **`POST /batches`** — parse + validate mọi URL **đồng bộ** trước (báo lỗi ngay, không tạo
   batch rồi mới fail), dedupe, tạo rows, `create_task`.
4. **`batch.html` + `_rows.html`** + HTMX poll tự dừng khi xong.
5. **`product.html`** — so sánh ảnh + text, nút copy (`navigator.clipboard`, 3 dòng JS).
6. **Sửa tay + rewrite lại 1 sản phẩm.**
7. **Template editor** — lưới anchor + slider + preview HTMX debounce.
8. **Upload sticker** (dùng service Phase 3).
9. **Export 3 format.**
10. **Xoá batch** — xoá row + `delete_objects` trên R2. Hỏi xác nhận.
11. **Trang quota.**
12. **README**: cách chạy, cách backup (`data/app.db*`), cảnh báo pháp lý, cảnh báo Gemini free tier.

## Todo List

- [x] `run_batch` tuần tự + `stage` + counter + delay
- [x] Khôi phục job dở dang lúc startup
- [x] `POST /submit`: validate URL sync trước, dedupe, `create_task`
- [x] `index.html`: ô nhập link + submit + khối kết quả ngay dưới
- [x] `_results.html` + `_product.html` partial + HTMX poll tự dừng
- [x] Macro Jinja `copyable()` dùng lại cho mọi ô
- [x] JS copy ~10 dòng, **event delegation** + fallback `execCommand`
- [x] Title cũ/mới cạnh nhau, mỗi ô 1 nút copy
- [x] Description cũ/mới cạnh nhau, mỗi ô 1 nút copy
- [x] Bảng ảnh: ghép cặp cũ↔mới theo `position`, đủ số lượng, thumbnail 2 bên
- [x] Mỗi link ảnh mới có nút copy riêng
- [x] Nút "Copy tất cả link ảnh mới" (1 URL/dòng)
- [x] Ảnh lỗi hiện thông báo, KHÔNG đẩy lệch hàng
- [x] Retry sản phẩm failed
- [x] Cảnh báo nổi bật khi `rewrite_status='failed'`
- [x] Hiển thị `desc_source` + `fetched_via`
- [x] Sửa tay title/mô tả + lưu
- [x] Rewrite lại 1 sản phẩm
- [x] Template editor: lưới anchor 3×3 + slider
- [x] Preview live qua HTMX debounce 500ms
- [x] Upload sticker PNG
- [x] Export JSON / CSV / ZIP
- [x] Xoá batch + xoá object R2 + hỏi xác nhận
- [x] Trang quota (Scrape.do tháng / Gemini ngày / R2 GB)
- [x] README: chạy, backup, cảnh báo pháp lý, cảnh báo Gemini free tier

## Success Criteria

- [x] Sai mật khẩu → không vào được; đúng → vào thẳng giao diện chính
- [x] Dán 1 link → submit → kết quả hiện **ngay trên trang đó**, không phải điều hướng
- [x] Dán 5 link → tiến trình chạy realtime không cần refresh
- [x] Batch xong → polling tự dừng (tab Network không còn request)
- [x] Title cũ/mới và description cũ/mới hiện cạnh nhau, so sánh được bằng mắt
- [x] Sản phẩm 26 ảnh → hiện đủ **26 hàng**, trái ảnh gốc, phải ảnh mới + link
- [x] Ảnh #N gốc và ảnh #N mới nằm **đúng cùng một hàng**
- [x] Bấm nút copy bất kỳ → clipboard đúng nội dung, nút đổi "OK" rồi trở lại
- [x] Copy hoạt động cả với sản phẩm vừa được HTMX swap vào (event delegation)
- [x] "Copy tất cả link ảnh mới" → được đúng N dòng URL
- [x] Một ảnh lỗi → hàng đó hiện lỗi, các hàng khác vẫn khớp cặp đúng
- [x] 1 link sai định dạng trong 5 → báo lỗi ngay ở form, **không** batch nào được tạo
- [x] 1 sản phẩm fail → 4 cái còn lại vẫn xong, batch `done` với `failed=1`
- [x] Restart app giữa batch → job dở thành `failed`, bấm retry chạy lại được
- [x] Template editor: đổi anchor → preview cập nhật < 2s
- [x] Export JSON/CSV mở được, ZIP giải nén ra đủ ảnh
- [x] Xoá batch → object trên R2 biến mất (kiểm tra dashboard Cloudflare)
- [x] Trang quota hiện đúng 3 con số
- [x] Chạy end-to-end 5 link thật: ảnh có sticker trên R2, title/mô tả đã viết lại

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| `database is locked` khi job + web cùng ghi | Trung bình | Cao | WAL + `busy_timeout` (Phase 1); transaction ngắn; không mở transaction bao quanh HTTP call |
| App restart mất job | Cao (chạy local) | Thấp | Đánh dấu `failed` lúc startup + nút retry. Chấp nhận được ở app 1 người dùng |
| Batch lớn chạy quá lâu | Trung bình | Thấp | `max_links_per_batch=20`; ~30s/SP → 10 phút/batch |
| Poll 2s làm nặng khi nhiều tab | Thấp | Thấp | Polling tự dừng khi xong; 1 người dùng |
| **`navigator.clipboard` không chạy ngoài HTTPS/localhost** | **Cao** nếu mở qua IP LAN | **Cao** (mất tính năng chính) | Chạy qua `localhost`; ra ngoài dùng `cloudflared tunnel` (có HTTPS); fallback `document.execCommand('copy')` |
| Gắn listener trực tiếp lên nút → mất copy sau HTMX swap | Cao nếu làm sai | Cao | Event delegation trên `document`, không gắn từng nút |
| Ảnh cũ/mới xếp lệch cặp | Trung bình | Cao | Ghép theo `position`; ảnh lỗi vẫn chiếm chỗ hàng |
| Xoá batch không xoá R2 → rác tích luỹ | Trung bình | Trung bình | Xoá R2 trong cùng handler; trang quota hiện GB để phát hiện lệch |
| User bỏ qua cảnh báo rewrite failed → đăng bán sai | Trung bình | Cao | Badge nổi bật; export cũng có cột trạng thái rewrite |

## Security Considerations

- Jinja2 autoescape bật mặc định — **đừng** dùng `|safe` cho nội dung từ Amazon.
- Mọi route (trừ `/login`, `/healthz`) sau `require_login`.
- POST form có CSRF token (`itsdangerous` ký, nhúng hidden input). Ứng dụng 1 người dùng
  nhưng cookie-based auth vẫn dính CSRF.
- Preview template: giới hạn tần suất (debounce client + đếm server), timeout 5s.
- Export: giới hạn số sản phẩm/lần.
- Xoá batch: yêu cầu xác nhận, dùng POST/DELETE (không GET — tránh bị prefetch xoá nhầm).
- Bind `127.0.0.1`. Ra ngoài thì qua `cloudflared tunnel` (có HTTPS), không forward port router.

## Next Steps

Xong Phase 5 là app dùng được. Muốn nâng cấp khi vượt free tier:
[reference-full-scale/](./reference-full-scale/README.md).
