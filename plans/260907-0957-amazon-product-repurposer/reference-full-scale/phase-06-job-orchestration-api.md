# Phase 6 — Job Orchestration & REST API

**Priority:** P1 · **Effort:** 12h · **Status:** Pending
**Context:** [plan.md](./plan.md) · Phase [2](./phase-02-ingestion-scraper-adapter.md) · [3](./phase-03-image-pipeline.md) · [5](./phase-05-ai-rewrite-engine.md)

## Overview

Ghép Phase 2/3/5 thành pipeline chạy nền, expose REST API cho frontend, thêm auth + progress tracking.

## Key Insights

- Một sản phẩm mất **20–60s** (scrape 3–10s + 7 ảnh + 2 lời gọi LLM). Batch 100 link = tới 1 giờ. Bắt buộc async, không có lựa chọn khác.
- **Image pipeline và AI rewrite độc lập** → chạy song song. Tiết kiệm ~40% thời gian mỗi sản phẩm.
- **Job granularity: 1 job / sản phẩm**, không phải 1 job / batch. Lý do: 1 sản phẩm fail không kéo cả batch; retry được từng cái; progress mịn.
- Progress cần realtime. **SSE đơn giản hơn WebSocket** và đủ dùng (một chiều server→client). Có polling fallback.
- Idempotency: user bấm submit 2 lần, mạng retry, worker crash giữa chừng — không được tạo batch trùng hay tính tiền 2 lần.

## Requirements

### Functional
- `POST /batches` nhận ≤100 link, trả `batch_id` ngay (202 Accepted)
- Dedupe link trùng trong cùng batch trước khi enqueue
- Pipeline mỗi sản phẩm: scrape → (images ∥ rewrite) → persist
- Progress realtime qua SSE; polling fallback
- Retry thủ công: cả batch, hoặc từng sản phẩm
- Cancel batch đang chạy
- Auth: JWT cho web UI + API key cho tích hợp máy
- Export: JSON, CSV, ZIP ảnh

### Non-functional
- Submit trả về < 500ms
- Worker crash → job không mất (ARQ có `job_id` bền + retry)
- Idempotent: submit lại cùng payload trong 60s → trả batch cũ

## Architecture

### Job graph

```
enqueue_batch(batch_id)                        # 1 job, fan-out
  └── for each product:
        process_product(product_id)            # 1 job / sản phẩm
              ├── stage: scrape    → ProductData (cache-aware)
              ├── asyncio.gather(
              │     process_images(product, template),    # Phase 3
              │     rewrite_content(product, options),    # Phase 5
              │   )   # return_exceptions=True — 1 nhánh fail không kéo nhánh kia
              └── finalize: cập nhật status + bump batch counters
```

`process_product` là **một** ARQ job, bên trong dùng `asyncio.gather`. Không tách images/rewrite thành job riêng — thêm phức tạp (cần join step) mà không lợi gì ở quy mô này.

### ARQ config

```python
class WorkerSettings:
    functions = [enqueue_batch, process_product, retry_product]
    cron_jobs = [
        cron(cleanup_scrape_cache,   hour=None, minute=7),      # mỗi giờ
        cron(cleanup_orphan_objects, hour=3, minute=0),         # hằng ngày 03:00
        cron(archive_kept_batches,   hour=4, minute=0),
    ]
    redis_settings   = RedisSettings.from_dsn(settings.redis_url)
    max_jobs         = settings.worker_concurrency      # 8
    job_timeout      = 300                              # 5 phút / sản phẩm
    keep_result      = 3600
    max_tries        = 3
    retry_jobs       = True
    health_check_interval = 30
```

`_job_id = f"product:{product_id}"` → ARQ dedupe, enqueue lại cùng id trong khi đang chạy sẽ bị bỏ qua. Đây là cơ chế idempotency ở tầng queue.

### Cập nhật counter (tránh race)

Nhiều job cùng bump `batches.done_count` → phải atomic:
```sql
UPDATE batches
SET done_count   = done_count   + :done,
    failed_count = failed_count + :failed,
    status = CASE
        WHEN done_count + failed_count + :done + :failed >= total_count
        THEN CASE WHEN failed_count + :failed = 0 THEN 'completed'
                  WHEN done_count + :done = 0     THEN 'failed'
                  ELSE 'partial' END
        ELSE 'running' END,
    completed_at = CASE WHEN done_count + failed_count + :done + :failed >= total_count
                        THEN now() ELSE completed_at END
WHERE id = :batch_id;
```
Một câu UPDATE, không đọc-rồi-ghi. Không cần lock.

### API endpoints

| Method | Path | Mô tả |
|--------|------|-------|
| POST | `/api/v1/auth/login` | email + password → JWT |
| POST | `/api/v1/auth/refresh` | refresh token |
| GET | `/api/v1/me` | thông tin user + quota |
| POST | `/api/v1/batches` | tạo batch (202) |
| GET | `/api/v1/batches` | list, phân trang, filter status |
| GET | `/api/v1/batches/{id}` | chi tiết + progress |
| GET | `/api/v1/batches/{id}/events` | **SSE** stream progress |
| POST | `/api/v1/batches/{id}/retry` | retry sản phẩm failed |
| POST | `/api/v1/batches/{id}/cancel` | huỷ |
| DELETE | `/api/v1/batches/{id}` | xoá batch + ảnh R2 |
| GET | `/api/v1/batches/{id}/export?format=json\|csv\|zip` | export |
| GET | `/api/v1/products/{id}` | chi tiết sản phẩm |
| PATCH | `/api/v1/products/{id}` | sửa tay title/description |
| POST | `/api/v1/products/{id}/rewrite` | rewrite lại 1 sản phẩm |
| GET/POST/PUT/DELETE | `/api/v1/templates...` | Phase 4 |
| GET/POST | `/api/v1/assets...` | Phase 4 |

### Request tạo batch

```python
class CreateBatchRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    template_id: UUID | None = None            # None -> template mặc định của user
    source_authorization: Literal["owned","supplier_licensed",
                                  "affiliate_paapi","unverified"] = "unverified"
    options: BatchOptions = BatchOptions()

class BatchOptions(BaseModel):
    rewrite_title: bool = True
    rewrite_description: bool = True
    tone: Literal["neutral","marketing","concise"] = "neutral"
    force_refresh: bool = False                # bỏ qua scrape cache
    emit_jpeg_fallback: bool = False
```

Xử lý:
1. Validate + parse mọi URL **đồng bộ** (nhanh) → báo lỗi ngay URL nào sai, không enqueue rồi mới fail.
2. Dedupe theo `(asin, marketplace)` trong batch.
3. Idempotency: hash `(user_id, sorted(urls), template_id, options)`. Nếu tồn tại batch cùng hash tạo trong 60s → trả batch đó, không tạo mới.
4. Tạo `batches` + N `products` trong **một transaction**.
5. Enqueue `enqueue_batch` **sau khi commit** (không enqueue trong transaction — worker có thể đọc trước khi commit xong).
6. Trả 202 + `batch_id`.

### SSE

```python
@router.get("/batches/{batch_id}/events")
async def stream(batch_id: UUID, user = Depends(current_user)):
    async def gen():
        last = None
        while True:
            snap = await get_batch_snapshot(batch_id, user.id)
            if snap != last:
                yield f"data: {snap.model_dump_json()}\n\n"
                last = snap
            if snap.status in TERMINAL:
                yield "event: done\ndata: {}\n\n"; return
            await asyncio.sleep(1.5)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
```

Poll DB mỗi 1.5s, chỉ gửi khi có thay đổi. Đơn giản, đủ dùng ở quy mô này. Redis pub/sub là tối ưu hoá cho sau — không cần ở 2000/ngày.

Timeout cứng 30 phút để tránh connection rò rỉ.

### Auth

- **JWT** cho web: access token 30 phút + refresh token 30 ngày (lưu HttpOnly cookie).
- **API key** cho máy: header `X-API-Key`, so `sha256` với `users.api_key_hash`. Hiện key raw **một lần duy nhất** lúc tạo.
- Dependency `current_user` chấp nhận cả hai.
- Password: `bcrypt` qua `passlib`.
- Mọi truy vấn batch/product/template lọc theo `user_id` **ở tầng repository**, không dựa vào route nhớ thêm `WHERE`.

### Export

- **JSON**: đầy đủ, gồm `orig_*`, `new_*`, danh sách CDN URL, `rewrite_flags`.
- **CSV**: 1 dòng/sản phẩm, cột ảnh `image_1..image_N` (N = max trong batch).
- **ZIP**: stream ảnh từ R2 → zip → response. **Không** load hết vào RAM: dùng `zipstream-ng` hoặc generator. Batch 100 SP × 7 ảnh × 150KB = 105MB — không được buffer.

## Related Code Files

**Create:**
- `apps/api/src/app/jobs/__init__.py`
- `apps/api/src/app/jobs/batch.py`            — `enqueue_batch`
- `apps/api/src/app/jobs/product.py`          — `process_product`, `retry_product`
- `apps/api/src/app/jobs/maintenance.py`      — cron jobs
- `apps/api/src/app/services/batches/service.py`
- `apps/api/src/app/services/batches/progress.py`
- `apps/api/src/app/services/export/json_export.py`
- `apps/api/src/app/services/export/csv_export.py`
- `apps/api/src/app/services/export/zip_export.py`
- `apps/api/src/app/core/auth.py`             — JWT + API key + `current_user`
- `apps/api/src/app/api/routes/auth.py`
- `apps/api/src/app/api/routes/batches.py`
- `apps/api/src/app/api/routes/products.py`
- `apps/api/src/app/api/deps.py`
- `apps/api/tests/test_batch_flow.py`         — end-to-end với MockProvider
- `apps/api/tests/test_auth.py`

**Modify:**
- `apps/api/src/app/worker.py` — đăng ký functions + cron
- `apps/api/src/app/main.py`   — mount routers, exception handlers

## Implementation Steps

1. **`core/auth.py`** — JWT issue/verify, API key hash/verify, `current_user` dependency chấp nhận cả hai.
2. **Repository layer với `user_id` scoping bắt buộc.** Mọi hàm `get_batch(id, user_id)`, không có overload bỏ `user_id`.
3. **`batches/service.py::create_batch`** — parse URL sync, dedupe, idempotency hash, transaction, enqueue sau commit.
4. **`jobs/product.py::process_product`** — pipeline đầy đủ với `asyncio.gather(..., return_exceptions=True)`, cập nhật `stage` từng bước để UI hiện đang làm gì.
5. **Counter update atomic** bằng một câu UPDATE.
6. **`jobs/batch.py::enqueue_batch`** — fan-out với `_job_id` deterministic.
7. **SSE endpoint** + snapshot builder + timeout.
8. **Retry/cancel** — retry chỉ enqueue lại sản phẩm `status='failed'`; cancel set `status='cancelled'` và `process_product` check cờ này ở đầu mỗi stage.
9. **Export 3 format** — ZIP phải stream.
10. **Cron maintenance** — dọn cache, dọn object mồ côi trên R2, archive batch `keep=true`.
11. **Test end-to-end** với `SCRAPER_PROVIDER=mock` + MinIO + mock OpenAI: submit 5 link → chờ → assert 5 sản phẩm completed, ảnh có trên MinIO, `new_title` khác `orig_title`.

## Todo List

- [ ] JWT issue/verify + refresh flow
- [ ] API key hash + verify, hiện raw 1 lần
- [ ] `current_user` dependency (JWT hoặc API key)
- [ ] Repository layer bắt buộc `user_id` scoping
- [ ] `create_batch`: parse sync → dedupe → idempotency hash → transaction → enqueue sau commit
- [ ] `process_product` với `asyncio.gather` images ∥ rewrite
- [ ] Cập nhật `stage` để UI hiện tiến trình chi tiết
- [ ] Counter update atomic một câu UPDATE
- [ ] `enqueue_batch` fan-out với `_job_id` deterministic
- [ ] SSE endpoint + snapshot + timeout 30 phút
- [ ] Retry batch / retry 1 sản phẩm
- [ ] Cancel batch + check cờ cancel ở đầu mỗi stage
- [ ] Export JSON
- [ ] Export CSV (cột ảnh động)
- [ ] Export ZIP **streaming** (không buffer)
- [ ] Cron: dọn scrape cache, dọn orphan R2, archive kept batches
- [ ] Exception handler → error code ổn định + `request_id`
- [ ] Test end-to-end với mock provider + MinIO + mock OpenAI

## Success Criteria

- [ ] `POST /batches` với 10 link trả 202 trong < 500ms
- [ ] SSE stream cập nhật progress mượt, kết thúc bằng `event: done`
- [ ] Submit cùng payload 2 lần trong 60s → trả cùng `batch_id`, chỉ 1 batch trong DB
- [ ] Kill worker giữa batch → restart → job tiếp tục, không sản phẩm nào mất
- [ ] 1 URL sai định dạng trong batch 10 → 400 ngay, **không** batch nào được tạo
- [ ] 1 sản phẩm scrape fail → 9 cái còn lại xong, batch status `partial`
- [ ] Rewrite fail nhưng ảnh ok → sản phẩm vẫn `completed`, `rewrite_status='failed'`
- [ ] User A không đọc được batch của user B (403/404, test tường minh)
- [ ] Export ZIP 100 sản phẩm → memory process không tăng quá 200MB (đo bằng `tracemalloc`)
- [ ] Cancel batch đang chạy → job đang chạy dừng ở stage tiếp theo, không sinh thêm chi phí

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| Race condition khi bump counter | Cao nếu làm sai | Trung bình | Atomic UPDATE một câu, không read-modify-write |
| Enqueue trước commit → worker đọc không thấy row | Trung bình | Cao | Enqueue **sau** `session.commit()`, tường minh trong code + comment |
| Job treo → chiếm slot worker vĩnh viễn | Trung bình | Cao | `job_timeout=300`; timeout riêng cho mỗi HTTP call bên trong |
| ZIP export OOM | Trung bình | Cao | Streaming bắt buộc; test có assert memory |
| SSE connection rò rỉ | Trung bình | Trung bình | Timeout 30 phút; đóng khi client disconnect (`request.is_disconnected()`) |
| Retry vô hạn khi provider chết | Trung bình | Cao | `max_tries=3`; circuit breaker ở provider adapter |
| IDOR — user đọc data user khác | Trung bình | **Cao** | `user_id` scoping ở repository layer, không ở route; test tường minh |

## Security Considerations

- **IDOR là lỗ hổng dễ mắc nhất ở phase này.** Scoping ở repository layer, không tin route nhớ thêm điều kiện. Viết test cho từng endpoint.
- Rate limit: `POST /batches` 10 req/phút/user; `/templates/preview` 20 req/phút.
- JWT secret riêng dev/prod; access token ngắn (30 phút).
- Refresh token trong HttpOnly + Secure + SameSite=Lax cookie, không localStorage.
- Không leak stack trace ra response production — trả error code + `request_id`, chi tiết chỉ trong log.
- Export ZIP: giới hạn số sản phẩm/lần (500) chống DoS.
- Xoá batch phải xoá cả object R2 — nếu không, dữ liệu bản quyền còn tồn tại sau khi user tưởng đã xoá.

## Next Steps

→ [Phase 7 — Frontend Application](./phase-07-frontend-application.md)
