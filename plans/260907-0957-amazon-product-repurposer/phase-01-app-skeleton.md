# Phase 1 — Khung app & Storage

**Priority:** P1 · **Effort:** 6h · **Status:** ✅ Done (2026-09-07)
**Context:** [plan.md](./plan.md) · [Research 02](./research/research-02-free-tier-options.md)

## Overview

Dựng một process FastAPI phục vụ cả API lẫn HTML, SQLite làm DB, kết nối R2. Không logic nghiệp vụ.

## Key Insights

- **Một process, một file DB.** Không Docker cho DB, không Redis, không worker riêng. Backup = copy file `.db`.
- SQLite cần bật **WAL mode** — mặc định `journal_mode=DELETE` khoá ghi rất chặt, background job + web request cùng ghi sẽ `database is locked`.
- SQLite async: `aiosqlite`. Vẫn chỉ có **một** writer tại một thời điểm → giữ transaction ngắn, không mở transaction bao quanh HTTP call.
- R2 dùng `boto3` (sync) trong threadpool là đủ — không cần `aioboto3` ở quy mô này. Ít dependency hơn.

## Requirements

- `uv run dev` là chạy được, không cần Docker
- SQLite WAL, schema tạo bằng SQL thuần (không Alembic — over-engineering cho 1 người dùng)
- Upload/đọc được file trên R2
- Bảo vệ bằng 1 password trong env, session cookie
- Trang `/` render được HTML với Tailwind CDN + HTMX

## Architecture

```
get-amazon-info/
├── app/
│   ├── main.py              # FastAPI + routes + lifespan
│   ├── config.py            # pydantic-settings
│   ├── db.py                # aiosqlite + schema init + helpers
│   ├── auth.py              # 1 password + session cookie
│   ├── storage.py           # R2 client
│   ├── models.py            # Pydantic DTOs
│   ├── services/            # Phase 2-4 điền vào
│   ├── templates/
│   │   ├── base.html
│   │   └── index.html
│   └── static/
├── data/
│   ├── app.db               # SQLite (gitignored)
│   └── fixtures/            # HTML mẫu cho test
├── tests/
├── .env.example
├── pyproject.toml
└── README.md
```

### Dependencies (cố ý ít)

```toml
dependencies = [
  "fastapi", "uvicorn[standard]", "jinja2", "python-multipart",
  "pydantic-settings", "aiosqlite", "httpx", "selectolax",
  "pillow", "boto3", "google-genai", "itsdangerous",
]
[dependency-groups]
dev = ["pytest", "pytest-asyncio", "respx", "ruff"]
```

Không có: SQLAlchemy, Alembic, Redis, ARQ, Celery, Sentry, prometheus. Cố ý.

### Schema (`db.py`, SQL thuần)

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;      -- chờ 5s thay vì fail ngay khi bị khoá

CREATE TABLE IF NOT EXISTS batches (
  id           TEXT PRIMARY KEY,            -- uuid4 hex
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
  total        INTEGER NOT NULL DEFAULT 0,
  done         INTEGER NOT NULL DEFAULT 0,
  failed       INTEGER NOT NULL DEFAULT 0,
  template_id  TEXT,
  options      TEXT NOT NULL DEFAULT '{}',  -- JSON
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS products (
  id            TEXT PRIMARY KEY,
  batch_id      TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  source_url    TEXT NOT NULL,
  asin          TEXT,
  marketplace   TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
  stage         TEXT,                             -- fetch|images|rewrite
  error         TEXT,
  fetched_via   TEXT,                             -- direct | scrapedo | cache
  -- gốc
  orig_title    TEXT,
  orig_desc     TEXT,
  bullets       TEXT,                             -- JSON array
  brand         TEXT,
  price         TEXT,
  desc_source   TEXT,                             -- product_description|bullets|aplus|none
  is_best_seller    INTEGER NOT NULL DEFAULT 0,
  has_free_delivery INTEGER NOT NULL DEFAULT 0,
  -- viết lại
  new_title     TEXT,
  new_desc      TEXT,
  rewrite_status TEXT,                            -- ok|skipped|failed
  rewrite_flags TEXT,                             -- JSON
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_products_batch ON products(batch_id);

CREATE TABLE IF NOT EXISTS images (
  id          TEXT PRIMARY KEY,
  product_id  TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  position    INTEGER NOT NULL,
  source_url  TEXT NOT NULL,
  r2_key      TEXT,
  cdn_url     TEXT,
  width       INTEGER, height INTEGER, bytes INTEGER,
  status      TEXT NOT NULL DEFAULT 'pending',
  error       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_images_pos ON images(product_id, position);

-- Cache HTML đã fetch — thứ bảo vệ quota Scrape.do 1000/tháng
CREATE TABLE IF NOT EXISTS html_cache (
  key        TEXT PRIMARY KEY,        -- "{marketplace}:{asin}"
  html       TEXT NOT NULL,
  via        TEXT NOT NULL,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS templates (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0,
  layers     TEXT NOT NULL DEFAULT '[]',   -- JSON
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assets (
  id      TEXT PRIMARY KEY,
  name    TEXT NOT NULL,
  r2_key  TEXT NOT NULL,
  cdn_url TEXT NOT NULL,
  width   INTEGER NOT NULL,
  height  INTEGER NOT NULL
);
```

Schema chạy bằng `executescript()` lúc startup. `CREATE TABLE IF NOT EXISTS` → idempotent.
Đổi schema sau này: viết hàm `migrate()` kiểm tra `PRAGMA user_version`. Không cần Alembic.

### Config

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_path: str = "data/app.db"
    app_password: str                       # bảo vệ app, 1 mật khẩu
    session_secret: str

    # Fetch
    scrapedo_token: str | None = None       # None -> chỉ fetch trực tiếp
    fetch_direct_first: bool = True
    html_cache_hours: int = 24
    request_delay_seconds: float = 2.0      # tự rate-limit, tránh bị chặn

    # Gemini
    gemini_api_key: str
    gemini_model: str = "gemini-flash-latest"   # KHÔNG hardcode version

    # R2
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_public_base_url: str                 # https://cdn.example.com hoặc r2.dev URL

    # Limits
    max_links_per_batch: int = 20
    max_images_per_product: int = 12
    max_image_bytes: int = 12_000_000
```

### Auth — đơn giản nhất có thể

```python
# 1 password trong env. Đúng -> set signed cookie (itsdangerous). Hết.
# Không user table, không JWT, không refresh token.
# Dependency require_login() đọc cookie, sai -> redirect /login
```

Dùng `secrets.compare_digest()` để so password (tránh timing attack — 1 dòng, cứ làm).

### R2 client

```python
# storage.py
_client = boto3.client("s3",
    endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
    aws_access_key_id=..., aws_secret_access_key=...,
    region_name="auto")

async def put(key: str, data: bytes, content_type: str) -> str:
    """Upload nếu chưa có. Trả CDN URL."""
    # head_object trước -> đã có thì bỏ qua (tiết kiệm quota Class A)
    # chạy trong asyncio.to_thread vì boto3 là sync
    return f"{settings.r2_public_base_url}/{key}"
```

## Related Code Files

**Create:**
- `pyproject.toml`, `.env.example`, `.gitignore`, `README.md`
- `app/config.py`, `app/db.py`, `app/auth.py`, `app/storage.py`, `app/models.py`
- `app/main.py`
- `app/templates/base.html`, `app/templates/index.html`, `app/templates/login.html`
- `tests/test_db.py`, `tests/test_storage.py`

## Implementation Steps

1. `uv init`, thêm deps ở trên. `.gitignore`: `.env`, `data/*.db*`, `__pycache__`, `.venv`.
2. `config.py` — fail fast nếu thiếu env bắt buộc, in ra tên biến nào thiếu.
3. `db.py` — `aiosqlite` connection dùng chung ở lifespan, `executescript(SCHEMA)` lúc startup, bật WAL + `busy_timeout`. Helper `fetch_one/fetch_all/execute`.
4. `auth.py` — `/login` form, `compare_digest`, signed cookie qua `itsdangerous.URLSafeSerializer`, dependency `require_login`.
5. `storage.py` — boto3 client, `put()` với `head_object` check trước, `asyncio.to_thread`.
6. `base.html` — Tailwind CDN + HTMX CDN, nav, flash message. `index.html` placeholder.
7. `main.py` — lifespan mở/đóng DB + httpx client, mount static, include routes, `/healthz`.
8. Script `scripts/check_setup.py` — verify mọi env var, ping R2, ping Gemini, in ✅/❌ từng cái. Chạy trước khi code Phase 2, tiết kiệm cả buổi debug.

## Todo List

- [x] `uv init` + deps (đúng danh sách trên, không thêm)
- [x] `.env.example` đầy đủ + `.gitignore`
- [x] `config.py` fail fast, báo rõ biến thiếu
- [x] `db.py`: WAL + `busy_timeout` + schema idempotent + helpers
- [x] `PRAGMA user_version` cho migration về sau
- [x] `auth.py`: login form + `compare_digest` + signed cookie + `require_login`
- [x] `storage.py`: R2 put với `head_object` check, chạy trong thread
- [x] `base.html` + `index.html` + `login.html` (Tailwind + HTMX CDN)
- [x] `main.py` lifespan + routes + `/healthz`
- [x] `scripts/check_setup.py` verify toàn bộ env + ping dịch vụ ngoài

## Sai lệch so với thiết kế (có chủ ý)

**Bỏ Tailwind CDN, dùng CSS tự viết với design token** (`app/static/app.css`, 277 dòng).
Lý do: Tailwind Play CDN nhét ~300KB compiler JS vào trình duyệt và in warning
"không dùng cho production"; với app Jinja vài trang thì class ngữ nghĩa
(`.card`, `.field`, `.copy-btn`) gọn hơn utility-first và kiểm soát typography tốt hơn.
Vẫn không có build step. → Snippet Tailwind ở Phase 5 cần đổi sang class ngữ nghĩa.

**Thêm bảng `quota`** (không có trong schema gốc): đếm request Scrape.do theo tháng và
Gemini theo ngày. Cần từ Phase 2 để không hết quota mà không biết.

**Thêm CSRF token** cho form POST — app dùng cookie auth nên vẫn dính CSRF dù 1 người dùng.

## Success Criteria

- [x] `uv run uvicorn app.main:app --reload` chạy, mở `localhost:8000` thấy trang login
- [x] Nhập sai password → báo lỗi; đúng → vào được `/`
- [x] `data/app.db` được tạo, `PRAGMA journal_mode` trả `wal`
- [x] Chạy lại app lần 2 → không lỗi schema (idempotent)
- [x] `scripts/check_setup.py` báo ✅ cho R2, Gemini, Scrape.do (hoặc ❌ rõ ràng nếu thiếu key)
- [x] Upload file test lên R2, mở được qua CDN URL trên trình duyệt
- [x] Upload lại đúng file đó → không gọi `put_object` lần 2 (in log chứng minh)

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| `database is locked` khi job nền + web cùng ghi | **Cao** nếu quên WAL | Cao | WAL + `busy_timeout=5000`; transaction ngắn; **không** mở transaction bao quanh HTTP call |
| Quên gitignore `.env` → lộ key | Thấp | Cao | `.gitignore` ngay bước 1; `git status` kiểm tra trước commit đầu |
| R2 custom domain chưa setup | Trung bình | Thấp | Dùng tạm `r2.dev` public URL cho dev (có rate limit, chấp nhận được ở dev) |
| SQLite file mất khi đổi máy | Trung bình | Trung bình | Backup = copy `data/app.db*` (3 file với WAL). Ghi vào README |

## Security Considerations

- `.env` không commit. Kiểm tra `git status` trước commit đầu tiên.
- Password so bằng `secrets.compare_digest`, không `==`.
- Cookie: `httponly=True`, `samesite="lax"`, `secure=True` khi chạy qua HTTPS tunnel.
- R2 token scope tối thiểu: Object Read & Write, chỉ bucket này.
- Không bật R2 public bucket listing.
- App chỉ bind `127.0.0.1` khi chạy local. Muốn ra ngoài thì qua `cloudflared tunnel` (có HTTPS), **không** bind `0.0.0.0` rồi forward port router.

## Next Steps

→ [Phase 2 — Lấy & Parse dữ liệu Amazon](./phase-02-fetch-and-parse.md)
