# Phase 8 — Hardening & Deployment

**Priority:** P2 · **Effort:** 8h · **Status:** Pending
**Context:** [plan.md](./plan.md) · tất cả phase trước

## Overview

Đưa lên production an toàn: giới hạn chi phí, quan sát được, chịu lỗi, có runbook.

## Key Insights

- **Cost control là tính năng, không phải tuỳ chọn.** Bug vòng lặp retry có thể đốt hàng trăm đô OpenAI qua một đêm. Cần hard cap ở tầng ứng dụng, không chỉ trông vào budget alert của nhà cung cấp (báo sau khi đã tiêu).
- **Circuit breaker** cho scraping provider: Amazon siết → provider trả `blocked` hàng loạt → không có breaker thì hệ thống vẫn đốt tiền gọi request chắc chắn fail.
- Metric quan trọng nhất không phải uptime mà là **tỉ lệ `rewrite_status='failed'`** và **tỉ lệ `description_source='none'`**. Hai chỉ số này tăng đột biến = provider đổi schema hoặc model đổi hành vi.

## Requirements

### Functional
- Hard cap chi phí ngày/tháng cho scraping và OpenAI, per-user và global
- Circuit breaker cho mỗi provider bên ngoài
- Rate limit mọi endpoint tốn kém
- Structured logging có `request_id` / `batch_id` / `product_id` xuyên suốt
- Sentry cho exception
- `/metrics` Prometheus
- Graceful shutdown cho worker (job đang chạy được hoàn tất hoặc requeue)
- Runbook cho 6 sự cố thường gặp

### Non-functional
- Deploy zero-downtime cho `api` và `web`
- Migration chạy tự động, an toàn khi rollback

## Architecture

### Cost guard

```python
# services/cost/guard.py
class CostGuard:
    async def check_and_reserve(self, user_id: UUID, kind: str, est_usd: float):
        """kind: 'scrape' | 'llm'. Raise BudgetExceeded nếu vượt."""
        # Redis counter, key theo ngày: cost:{kind}:{user_id}:{YYYYMMDD}
        # + key global: cost:{kind}:global:{YYYYMMDD}
        # INCRBYFLOAT + EXPIRE 48h
```

Ngưỡng trong config:
```python
daily_budget_scrape_usd_user: float = 5.0
daily_budget_llm_usd_user: float = 10.0
daily_budget_scrape_usd_global: float = 30.0
daily_budget_llm_usd_global: float = 50.0
```

Vượt → batch dừng ở `status='paused_budget'`, hiện rõ trong UI, gửi alert. **Không** fail âm thầm.

Đối chiếu: cuối ngày cron so tổng Redis với `SUM(scrape_cost_usd + llm_cost_usd)` từ Postgres, log chênh lệch.

### Circuit breaker

```python
# 3 trạng thái: closed -> open -> half_open
# open khi: >=5 lỗi liên tiếp HOẶC tỉ lệ lỗi >50% trong 20 request gần nhất
# open trong 60s -> half_open (cho qua 1 request thử) -> ok thì closed
# State trong Redis (chia sẻ giữa nhiều worker instance)
```

Áp cho: Oxylabs, Scrapingdog, OpenAI, R2.

Provider scraping `open` → thử fallback provider nếu có cấu hình; không có → batch `paused_provider`.

### Rate limit

| Endpoint | Giới hạn |
|----------|----------|
| `POST /batches` | 10/phút/user |
| `POST /templates/preview` | 20/phút/user |
| `POST /assets` | 10/phút/user |
| `GET /batches/{id}/export` | 5/phút/user |
| `POST /auth/login` | 5/phút/IP |
| toàn cục mỗi user | 300/phút |

`slowapi` hoặc middleware tự viết với Redis. Trả `429` + header `Retry-After`.

### Metrics (`/metrics`)

```
batch_created_total{user}
product_processed_total{status}
product_duration_seconds{stage}          # histogram: scrape|images|rewrite
image_processed_total{status}
rewrite_validator_total{severity}        # ok|warn|fail  <- theo dõi sát
scrape_description_source_total{source}  # <- theo dõi sát, none tăng = schema vỡ
external_call_duration_seconds{provider}
circuit_breaker_state{provider}
cost_usd_total{kind}
queue_depth
```

### Alert (Sentry / webhook)

| Điều kiện | Mức |
|-----------|-----|
| Circuit breaker `open` | Cảnh báo |
| `rewrite_validator_total{severity="fail"}` > 20% trong 1h | Cảnh báo |
| `scrape_description_source_total{source="none"}` > 30% trong 1h | **Nghiêm trọng** — schema có thể đã vỡ |
| Chi phí ngày đạt 80% ngưỡng | Cảnh báo |
| `queue_depth` > 500 trong 15 phút | Cảnh báo |
| Dùng model fallback (model chính không tồn tại) | **Nghiêm trọng** |

### Graceful shutdown

ARQ nhận `SIGTERM` (Railway gửi khi redeploy):
- Ngừng nhận job mới
- Chờ job đang chạy tối đa 60s
- Quá 60s → requeue (ARQ tự làm nếu job chưa ack)

FastAPI: `lifespan` đóng httpx client, aioboto3 session, ProcessPoolExecutor sạch.

### Deploy Railway

| Service | Root | Command |
|---------|------|---------|
| `api` | `apps/api` | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| `worker` | `apps/api` | `arq app.worker.WorkerSettings` |
| `web` | `apps/web` | `next start -p $PORT` |
| Postgres | managed | — |
| Redis | managed | — |

- Release command trên `api`: `alembic upgrade head`
- Health check path: `/healthz`, timeout 30s
- `worker` **không** có public domain
- Dùng Dockerfile tự viết cho `api` (Nixpacks hay lỗi với Pillow) — cài `libjpeg-dev libwebp-dev zlib1g-dev libpng-dev`

**Quy tắc migration an toàn:** chỉ thêm cột nullable / bảng mới trong cùng deploy với code. Xoá cột làm ở deploy sau (expand-contract). Nếu không, rollback code sẽ vỡ vì schema đã đổi.

### Runbook (`docs/runbook.md`)

1. **Scrape fail hàng loạt** → check circuit breaker state → check status page provider → đổi `SCRAPER_PROVIDER` sang fallback → redeploy worker
2. **Chi phí tăng bất thường** → query `SUM(llm_cost_usd) GROUP BY DATE` → kiểm tra tỉ lệ retry → hạ `daily_budget_*`
3. **Queue tồn đọng** → tăng `worker_concurrency` hoặc scale worker replica → check job treo
4. **`description_source='none'` tăng vọt** → provider đổi schema → so `raw_payload` gần đây với fixture → sửa normalizer → reprocess từ `raw_payload` (không scrape lại)
5. **Model OpenAI bị deprecate** → cập nhật `OPENAI_MODEL_*` env → redeploy (không cần đổi code)
6. **Storage R2 phình** → check lifecycle rule còn hoạt động → chạy `cleanup_orphan_objects` thủ công

## Related Code Files

**Create:**
- `apps/api/src/app/services/cost/guard.py`
- `apps/api/src/app/services/resilience/circuit_breaker.py`
- `apps/api/src/app/core/rate_limit.py`
- `apps/api/src/app/core/metrics.py`
- `apps/api/src/app/api/routes/metrics.py`
- `apps/api/src/app/core/sentry.py`
- `apps/api/Dockerfile`
- `apps/web/Dockerfile`
- `docs/runbook.md`
- `docs/deployment.md`
- `.github/workflows/deploy.yml`

**Modify:**
- `apps/api/src/app/worker.py` — graceful shutdown, circuit breaker wiring
- mọi service gọi API ngoài — bọc circuit breaker + cost guard

## Implementation Steps

1. **`circuit_breaker.py`** — state trong Redis, decorator `@with_breaker("oxylabs")`. Test bằng cách mock lỗi liên tiếp.
2. **`cost/guard.py`** — Redis counter, check trước khi gọi, ghi thật sau khi gọi. Cron đối chiếu với Postgres.
3. **Bọc guard + breaker** vào scraper client và OpenAI client.
4. **`rate_limit.py`** — middleware Redis, áp theo bảng trên.
5. **`metrics.py`** — `prometheus_client`, expose `/metrics` (bảo vệ bằng token hoặc chỉ internal network).
6. **Sentry** — init ở `main.py` + `worker.py`, scrub PII, `traces_sample_rate=0.1`.
7. **Structured logging** — middleware gán `request_id`; worker gán `batch_id`/`product_id` vào contextvar.
8. **Graceful shutdown** — signal handler ARQ, lifespan FastAPI.
9. **Dockerfile** cho api (multi-stage, non-root user, system deps cho Pillow) và web (standalone output).
10. **Deploy workflow** — CI pass → Railway deploy; migration qua release command.
11. **`docs/runbook.md`** + `docs/deployment.md`.
12. **Smoke test production** — script chạy 1 batch 3 link thật sau mỗi deploy, assert kết quả.

## Todo List

- [ ] Circuit breaker Redis-backed + decorator + test
- [ ] Cost guard: check trước / ghi sau / cron đối chiếu
- [ ] Bọc guard + breaker vào scraper + OpenAI + R2
- [ ] Trạng thái `paused_budget` / `paused_provider` + hiển thị UI
- [ ] Rate limit middleware theo bảng
- [ ] `/metrics` Prometheus với đủ metric ở bảng trên
- [ ] Sentry init + PII scrubbing
- [ ] Structured logging với context xuyên suốt request/job
- [ ] Alert rules (Sentry / webhook)
- [ ] Graceful shutdown worker + FastAPI lifespan
- [ ] Dockerfile api (multi-stage, non-root, system deps Pillow)
- [ ] Dockerfile web (standalone)
- [ ] Deploy workflow GitHub Actions → Railway
- [ ] Release command `alembic upgrade head`
- [ ] `docs/runbook.md` 6 kịch bản
- [ ] `docs/deployment.md`
- [ ] Smoke test script chạy sau deploy
- [ ] `gitleaks` + `pip-audit` + `npm audit` trong CI

## Success Criteria

- [ ] Vượt daily budget → batch `paused_budget`, có alert, **không** tiếp tục tiêu tiền
- [ ] Mock provider fail 5 lần liên tiếp → breaker `open`, request tiếp theo fail nhanh (không gọi mạng)
- [ ] `/metrics` trả đủ metric, Prometheus scrape được
- [ ] Redeploy worker giữa batch → job đang chạy hoàn tất hoặc requeue, **không** mất
- [ ] Exception → xuất hiện trong Sentry kèm `request_id`, không có PII/secret
- [ ] Rate limit trả `429` + `Retry-After` đúng
- [ ] Deploy production: api + web + worker xanh, smoke test pass
- [ ] `gitleaks` không phát hiện secret trong repo
- [ ] Rollback deploy trước → app vẫn chạy (chứng minh migration tương thích ngược)

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| Bug vòng lặp đốt hết ngân sách OpenAI | Trung bình | **Rất cao** | Hard cap ở app + budget alert nhà cung cấp; giới hạn 2 retry |
| Migration phá production | Trung bình | Cao | Expand-contract; test `upgrade` + `downgrade` trong CI trên DB có dữ liệu |
| Circuit breaker sai → chặn nhầm khi provider vẫn ổn | Thấp | Trung bình | Ngưỡng đủ cao (5 lỗi liên tiếp); half-open thử lại 60s |
| `/metrics` lộ thông tin nội bộ | Trung bình | Trung bình | Bảo vệ bằng bearer token hoặc chỉ internal network |
| Railway build fail vì Pillow thiếu system deps | Trung bình | Trung bình | Dockerfile tự viết, không dùng Nixpacks |
| Secret lọt vào git | Thấp | **Rất cao** | `gitleaks` trong CI + pre-commit hook |

## Security Considerations

- `gitleaks` chặn commit chứa secret (pre-commit + CI).
- `pip-audit` + `npm audit` trong CI, fail khi có CVE mức High trở lên. Pillow đặc biệt cần theo dõi.
- Docker image chạy bằng **non-root user**.
- `/metrics` không public.
- Sentry `before_send` scrub: `authorization`, `api_key`, `password`, `openai_api_key`, email.
- Xoay `jwt_secret` và API key provider định kỳ (ghi lịch trong runbook).
- Backup Postgres: bật point-in-time recovery của Railway; test restore **một lần** trước khi lên production thật.

## Next Steps

Chạy `/ck:plan red-team` để review đối kháng trước khi bắt đầu code.
Sau khi hoàn thành: `/ck:journal` ghi lại quyết định kiến trúc.
