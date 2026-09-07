# Phase 1 — Foundation & Infrastructure

**Priority:** P1 · **Effort:** 12h · **Status:** Pending
**Context:** [plan.md](./plan.md) · [Research 01](./research/research-01-providers-and-costs.md)

## Overview

Dựng bộ khung monorepo, database schema, config, và Railway services. Không có logic nghiệp vụ — chỉ hạ tầng để các phase sau cắm vào.

## Key Insights

- Monorepo 2 ngôn ngữ (TS + Python) → dùng thư mục phẳng, **không** dùng Nx/Turborepo (over-engineering cho 2 app).
- Schema phải thiết kế đúng ngay từ đầu vì Phase 3–6 đều đụng vào. Migration sau này tốn hơn nhiều.
- `source_authorization` phải có từ Phase 1 (ràng buộc pháp lý), không nhét sau.
- Railway cần **4 service riêng**: `web` (Next.js), `api` (FastAPI), `worker` (ARQ), + Postgres/Redis managed. Worker không share process với API.

## Requirements

### Functional
- Repo chạy được local bằng `docker compose up`
- Migration chạy được cả local và trên Railway
- Health check `/healthz` (liveness) và `/readyz` (Postgres + Redis + R2 reachable)

### Non-functional
- Mọi secret qua env var, `.env.example` đầy đủ, không commit `.env`
- Type checking bật strict: `mypy --strict` (Python), `tsc --strict` (TS)
- Pre-commit hook: ruff format + ruff check + mypy + eslint

## Architecture

```
get-amazon-info/
├── apps/
│   ├── web/                    # Next.js 15
│   │   ├── src/app/
│   │   ├── src/components/
│   │   ├── src/lib/api-client.ts
│   │   └── package.json
│   └── api/                    # FastAPI + ARQ worker (cùng codebase, khác entrypoint)
│       ├── src/app/
│       │   ├── main.py             # FastAPI entrypoint
│       │   ├── worker.py           # ARQ entrypoint
│       │   ├── config.py           # pydantic-settings
│       │   ├── db/
│       │   │   ├── session.py
│       │   │   └── models.py
│       │   ├── api/routes/
│       │   ├── services/           # Phase 2-5 điền vào
│       │   ├── schemas/            # Pydantic DTOs
│       │   └── core/
│       │       ├── logging.py
│       │       └── errors.py
│       ├── alembic/
│       ├── tests/
│       └── pyproject.toml
├── docker-compose.yml          # postgres + redis + api + worker + web
├── .env.example
├── docs/development-rules.md
└── plans/
```

### Database Schema (Postgres)

```sql
-- Người dùng (tối giản, mở rộng ở Phase 6)
CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT UNIQUE NOT NULL,
  hashed_password TEXT,
  api_key_hash  TEXT UNIQUE,
  role          TEXT NOT NULL DEFAULT 'member',  -- admin | member
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Một lần submit (có thể chứa nhiều link)
CREATE TABLE batches (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id),
  template_id   UUID REFERENCES sticker_templates(id),
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending|running|completed|failed|partial
  total_count   INT NOT NULL DEFAULT 0,
  done_count    INT NOT NULL DEFAULT 0,
  failed_count  INT NOT NULL DEFAULT 0,
  options       JSONB NOT NULL DEFAULT '{}',       -- rewrite tone, output format, locale...
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at  TIMESTAMPTZ
);

-- Một sản phẩm trong batch
CREATE TABLE products (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id      UUID NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  source_url    TEXT NOT NULL,
  asin          TEXT,
  marketplace   TEXT,                              -- amazon.com, amazon.de...
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending|scraping|processing|completed|failed
  stage         TEXT,                              -- scrape|images|rewrite  (stage đang chạy)
  error_code    TEXT,
  error_message TEXT,
  source_authorization TEXT NOT NULL DEFAULT 'unverified',
      -- owned | supplier_licensed | affiliate_paapi | unverified
  -- dữ liệu gốc
  raw_payload   JSONB,                             -- response thô từ provider (debug + reprocess)
  orig_title    TEXT,
  orig_description TEXT,
  orig_bullets  JSONB,
  brand         TEXT,
  price         NUMERIC(12,2),
  currency      TEXT,
  -- dữ liệu đã viết lại
  new_title     TEXT,
  new_description TEXT,
  rewrite_status TEXT,                             -- ok|degraded|skipped|failed
  rewrite_flags JSONB,                             -- cảnh báo từ Fidelity Validator
  -- chi phí
  scrape_cost_usd  NUMERIC(10,6) DEFAULT 0,
  llm_cost_usd     NUMERIC(10,6) DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ix_products_batch_asin ON products(batch_id, asin, marketplace)
  WHERE asin IS NOT NULL;
CREATE INDEX ix_products_status ON products(status);
CREATE INDEX ix_products_asin_market ON products(asin, marketplace);

-- Ảnh
CREATE TABLE product_images (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id    UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  position      INT NOT NULL,                      -- thứ tự gallery, 0 = ảnh chính
  source_url    TEXT NOT NULL,
  hires_url     TEXT,                              -- URL sau khi strip modifier block
  r2_key        TEXT,                              -- key trên R2
  cdn_url       TEXT,                              -- URL public qua custom domain
  width         INT,
  height        INT,
  bytes         INT,
  format        TEXT,                              -- webp | jpeg
  checksum_sha256 TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending|downloaded|processed|uploaded|failed
  error_message TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ix_images_product_position ON product_images(product_id, position);

-- Template sticker (Phase 4 dùng, tạo bảng từ Phase 1)
CREATE TABLE sticker_templates (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id),
  name          TEXT NOT NULL,
  is_default    BOOLEAN NOT NULL DEFAULT false,
  layers        JSONB NOT NULL DEFAULT '[]',       -- xem Phase 4 để biết schema
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Asset sticker/logo user upload
CREATE TABLE sticker_assets (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id),
  name          TEXT NOT NULL,
  r2_key        TEXT NOT NULL,
  cdn_url       TEXT NOT NULL,
  width         INT NOT NULL,
  height        INT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cache kết quả scrape (tránh gọi lại provider, tiết kiệm tiền)
CREATE TABLE scrape_cache (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  asin          TEXT NOT NULL,
  marketplace   TEXT NOT NULL,
  provider      TEXT NOT NULL,
  payload       JSONB NOT NULL,
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX ix_cache_key ON scrape_cache(asin, marketplace, provider);
CREATE INDEX ix_cache_expiry ON scrape_cache(expires_at);
```

### Config (`config.py`)

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    env: str = "development"
    database_url: str
    redis_url: str

    # Scraping
    scraper_provider: str = "oxylabs"          # oxylabs | scrapingdog | mock
    oxylabs_username: str | None = None
    oxylabs_password: str | None = None
    scrapingdog_api_key: str | None = None
    scrape_cache_ttl_hours: int = 24

    # Cloudflare R2
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_public_base_url: str                     # https://cdn.example.com

    # OpenAI  — LƯU Ý: gpt-5-mini EOL 2026-12-11, KHÔNG hardcode
    openai_api_key: str
    openai_model_title: str = "gpt-5-nano"
    openai_model_description: str = "gpt-5-mini"
    openai_model_fallback: str = "gpt-5-nano"
    openai_max_retries: int = 2

    # Limits
    max_links_per_batch: int = 100
    max_images_per_product: int = 15
    max_image_bytes: int = 15 * 1024 * 1024
    worker_concurrency: int = 8

    # Security
    jwt_secret: str
    cors_origins: list[str] = ["http://localhost:3000"]

settings = Settings()
```

## Related Code Files

**Create:**
- `/Users/macos/DATA/get-amazon-info/docker-compose.yml`
- `/Users/macos/DATA/get-amazon-info/.env.example`
- `/Users/macos/DATA/get-amazon-info/.gitignore`
- `/Users/macos/DATA/get-amazon-info/docs/development-rules.md`
- `/Users/macos/DATA/get-amazon-info/apps/api/pyproject.toml`
- `/Users/macos/DATA/get-amazon-info/apps/api/src/app/config.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/src/app/main.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/src/app/worker.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/src/app/db/models.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/src/app/db/session.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/src/app/core/logging.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/src/app/core/errors.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/src/app/api/routes/health.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/alembic/env.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/alembic/versions/0001_initial.py`
- `/Users/macos/DATA/get-amazon-info/apps/api/Dockerfile`
- `/Users/macos/DATA/get-amazon-info/apps/web/package.json`
- `/Users/macos/DATA/get-amazon-info/apps/web/Dockerfile`
- `/Users/macos/DATA/get-amazon-info/.github/workflows/ci.yml`

## Implementation Steps

1. **Khởi tạo repo**
   - `git init`, `.gitignore` (Python + Node + `.env` + `*.pyc` + `.venv`)
   - Viết `docs/development-rules.md`: quy ước đặt tên, error handling, không hardcode secret, mọi I/O async, test bắt buộc cho service layer.

2. **Scaffold FastAPI** (`apps/api`)
   - `uv init` hoặc Poetry. Deps: `fastapi`, `uvicorn[standard]`, `pydantic-settings`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `arq`, `httpx`, `aioboto3`, `pillow`, `openai`, `structlog`, `python-jose[cryptography]`, `passlib[bcrypt]`.
   - Dev deps: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`, `respx` (mock httpx), `moto` hoặc `minio` cho test S3.

3. **Config + logging**
   - `config.py` như trên. Fail fast nếu thiếu env bắt buộc.
   - `logging.py`: structlog JSON, có `request_id` + `batch_id` + `product_id` trong context.

4. **DB models + migration**
   - Viết SQLAlchemy 2.0 declarative models khớp schema trên.
   - `alembic init`, sửa `env.py` để đọc `settings.database_url` và autogenerate từ metadata.
   - Sinh migration `0001_initial`, kiểm tra SQL sinh ra đúng (đặc biệt partial unique index).

5. **Health endpoints**
   - `GET /healthz` → 200 luôn (liveness)
   - `GET /readyz` → check Postgres `SELECT 1`, Redis `PING`, R2 `head_bucket`. Trả 503 nếu fail, kèm chi tiết từng dependency.

6. **ARQ worker skeleton**
   - `worker.py` với `WorkerSettings`: redis_settings, `functions=[]` (Phase 6 điền), `max_jobs=settings.worker_concurrency`, `job_timeout=300`.
   - Đăng ký `on_startup` tạo shared httpx client + aioboto3 session (không tạo mới mỗi job).

7. **Scaffold Next.js** (`apps/web`)
   - `create-next-app` với TS + Tailwind + App Router.
   - Cài `shadcn/ui`, `@tanstack/react-query`, `zod`, `react-hook-form`.
   - `lib/api-client.ts`: typed fetch wrapper trỏ `NEXT_PUBLIC_API_URL`.
   - Trang `/` placeholder gọi `/healthz` để chứng minh kết nối.

8. **docker-compose**
   - Services: `postgres:16`, `redis:7`, `api`, `worker`, `web`. Health check cho postgres/redis. `api` và `worker` `depends_on` với `condition: service_healthy`.

9. **CI** (`.github/workflows/ci.yml`)
   - Job `api`: ruff check + ruff format --check + mypy + pytest (với postgres/redis service container)
   - Job `web`: tsc --noEmit + eslint + next build

10. **Railway setup**
    - Tạo project. Thêm Postgres + Redis plugin.
    - 3 service từ repo: `api` (root `apps/api`, cmd `uvicorn`), `worker` (root `apps/api`, cmd `arq app.worker.WorkerSettings`), `web` (root `apps/web`).
    - Set biến môi trường, dùng reference variable của Railway cho `DATABASE_URL`/`REDIS_URL`.
    - Chạy migration: release command `alembic upgrade head` trên service `api`.

11. **Tạo R2 bucket**
    - Bucket `amazon-repurposer-prod` + `amazon-repurposer-dev`.
    - API token scope: Object Read & Write, chỉ 2 bucket này.
    - Gắn custom domain `cdn.<domain>` cho public access. **Không** bật public bucket URL mặc định (`r2.dev`) trong production — bị rate limit.

## Todo List

- [ ] `git init` + `.gitignore` + `docs/development-rules.md`
- [ ] Scaffold `apps/api` với pyproject + deps
- [ ] `config.py` với pydantic-settings + `.env.example`
- [ ] `core/logging.py` structlog JSON + context vars
- [ ] `core/errors.py`: exception classes + FastAPI exception handlers
- [ ] SQLAlchemy models (7 bảng)
- [ ] Alembic init + migration `0001_initial` + verify SQL
- [ ] `/healthz` + `/readyz` với dependency checks
- [ ] ARQ worker skeleton + shared client lifecycle
- [ ] Scaffold `apps/web` Next.js 15 + Tailwind + shadcn + TanStack Query
- [ ] `docker-compose.yml` chạy được `docker compose up`
- [ ] CI workflow (lint + type + test cả 2 app)
- [ ] Railway project + Postgres + Redis + 3 service
- [ ] R2 bucket dev/prod + API token + custom domain

## Success Criteria

- [ ] `docker compose up` → cả 5 container healthy
- [ ] `curl localhost:8000/readyz` → 200 với tất cả dependency `ok`
- [ ] `alembic upgrade head` chạy sạch trên DB rỗng; `alembic downgrade base` cũng sạch
- [ ] `mypy --strict src/` → 0 lỗi; `tsc --noEmit` → 0 lỗi
- [ ] Frontend `localhost:3000` hiển thị được trạng thái API health
- [ ] Railway: 3 service deploy xanh, `/readyz` trả 200 qua public domain
- [ ] Test upload 1 file lên R2 bằng script, đọc lại qua CDN URL

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| Schema thiết kế thiếu, phải migration lớn ở Phase 5 | Trung bình | Cao | `raw_payload JSONB` giữ nguyên response thô → reprocess được mà không scrape lại |
| Railway build Python chậm/fail do Pillow cần lib hệ thống | Trung bình | Trung bình | Dùng Dockerfile tự viết (không Nixpacks), cài `libjpeg-dev libwebp-dev zlib1g-dev` |
| R2 custom domain chưa propagate → test fail | Thấp | Thấp | Dùng S3 endpoint trực tiếp cho test nội bộ, CDN URL chỉ để serve |
| `mypy --strict` gây ma sát lớn với SQLAlchemy 2.0 | Trung bình | Thấp | Dùng `sqlalchemy[mypy]` plugin; nới `--strict` riêng cho `db/models.py` nếu cần |

## Security Considerations

- Secret **chỉ** qua env var. `.env` trong `.gitignore`. Kiểm tra bằng `gitleaks` trong CI.
- R2 token scope tối thiểu (chỉ 2 bucket, không account-wide).
- `jwt_secret` sinh bằng `secrets.token_urlsafe(64)`, khác nhau giữa dev/prod.
- CORS whitelist tường minh, **không** `allow_origins=["*"]`.
- Postgres: user app không phải superuser, chỉ có quyền trên schema `public`.
- `raw_payload` có thể chứa dữ liệu bản quyền → không log ra stdout, chỉ lưu DB.

## Next Steps

→ [Phase 2 — Ingestion & Scraper Adapter](./phase-02-ingestion-scraper-adapter.md)

**Cần chốt với user cuối phase này:** auth model (single API key vs multi-user login) — ảnh hưởng Phase 6.
