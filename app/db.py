"""Postgres qua asyncpg.

Giữ nguyên style placeholder `?` của toàn bộ codebase — một adapter nhỏ đổi sang
`$1,$2...` của Postgres. Rẻ hơn nhiều so với sửa ~50 câu query rải khắp nơi.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Any

import asyncpg

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id          TEXT PRIMARY KEY,
  seq         BIGSERIAL,
  status      TEXT NOT NULL DEFAULT 'pending',
  total       INTEGER NOT NULL DEFAULT 0,
  done        INTEGER NOT NULL DEFAULT 0,
  failed      INTEGER NOT NULL DEFAULT 0,
  template_id TEXT,
  options     TEXT NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS products (
  id                TEXT PRIMARY KEY,
  seq               BIGSERIAL,
  batch_id          TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  source_url        TEXT NOT NULL,
  asin              TEXT,
  marketplace       TEXT,
  status            TEXT NOT NULL DEFAULT 'pending',
  stage             TEXT,
  error             TEXT,
  fetched_via       TEXT,
  orig_title        TEXT,
  orig_desc         TEXT,
  bullets           TEXT,
  brand             TEXT,
  price             TEXT,
  desc_source       TEXT,
  is_best_seller    INTEGER NOT NULL DEFAULT 0,
  has_free_delivery INTEGER NOT NULL DEFAULT 0,
  new_title         TEXT,
  new_desc          TEXT,
  rewrite_status    TEXT,
  rewrite_flags     TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_products_batch ON products(batch_id);
CREATE INDEX IF NOT EXISTS ix_products_status ON products(status);

CREATE TABLE IF NOT EXISTS images (
  id         TEXT PRIMARY KEY,
  product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  position   INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  r2_key     TEXT,
  cdn_url    TEXT,
  width      INTEGER,
  height     INTEGER,
  bytes      INTEGER,
  status     TEXT NOT NULL DEFAULT 'pending',
  error      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_images_pos ON images(product_id, position);

CREATE TABLE IF NOT EXISTS html_cache (
  key        TEXT PRIMARY KEY,
  html       TEXT NOT NULL,
  via        TEXT NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS templates (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0,
  layers     TEXT NOT NULL DEFAULT '[]',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assets (
  id      TEXT PRIMARY KEY,
  name    TEXT NOT NULL,
  r2_key  TEXT NOT NULL,
  cdn_url TEXT NOT NULL,
  width   INTEGER NOT NULL,
  height  INTEGER NOT NULL,
  data    BYTEA
);

CREATE TABLE IF NOT EXISTS quota (
  service TEXT NOT NULL,
  period  TEXT NOT NULL,
  used    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (service, period)
);
"""

_pool: asyncpg.Pool | None = None
_PLACEHOLDER = re.compile(r"\?")


def new_id() -> str:
    return uuid.uuid4().hex


def _convert(sql: str) -> str:
    """`?` -> `$1,$2,...`. Codebase viết theo style SQLite, Postgres cần $n."""
    n = 0

    def repl(_m: re.Match[str]) -> str:
        nonlocal n
        n += 1
        return f"${n}"

    return _PLACEHOLDER.sub(repl, sql)


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        settings.pg_dsn,
        min_size=1,
        max_size=settings.db_pool_size,
        command_timeout=60,
        # Neon scale-to-zero: connection cũ có thể chết khi compute ngủ.
        max_inactive_connection_lifetime=180,
        statement_cache_size=0,   # bắt buộc khi đi qua pgbouncer của Neon
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB chưa mở — connect() phải chạy ở lifespan startup")
    return _pool


async def fetch_one(sql: str, params: Iterable[Any] = ()) -> asyncpg.Record | None:
    async with pool().acquire() as conn:
        return await conn.fetchrow(_convert(sql), *tuple(params))


async def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[asyncpg.Record]:
    async with pool().acquire() as conn:
        return list(await conn.fetch(_convert(sql), *tuple(params)))


async def execute(sql: str, params: Iterable[Any] = ()) -> str:
    async with pool().acquire() as conn:
        return await conn.execute(_convert(sql), *tuple(params))


async def execute_many(statements: list[tuple[str, tuple]]) -> None:
    """Nhiều câu trong MỘT transaction — dùng khi ghi loạt ảnh của 1 sản phẩm."""
    async with pool().acquire() as conn, conn.transaction():
        for sql, params in statements:
            await conn.execute(_convert(sql), *params)


async def recover_stuck_jobs() -> int:
    """App restart giữa batch -> job kẹt ở 'running' mãi. Đánh dấu failed để retry.
    Trên Render free, service spin-down sau 15 phút không hoạt động -> gặp thường xuyên."""
    async with pool().acquire() as conn, conn.transaction():
        res = await conn.execute(
            "UPDATE products SET status='failed', error='app khởi động lại giữa chừng' "
            "WHERE status IN ('pending','running')"
        )
        await conn.execute(
            "UPDATE batches SET status='failed' WHERE status IN ('pending','running')"
        )
    return int(res.split()[-1]) if res.startswith("UPDATE") else 0
