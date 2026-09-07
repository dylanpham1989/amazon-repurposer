"""Cấu hình đọc từ .env. Fail fast + báo rõ biến nào thiếu."""

from __future__ import annotations

import sys

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Bảo vệ app ---
    app_password: str
    session_secret: str

    # --- Lấy dữ liệu Amazon ---
    # Tầng 1 (trực tiếp) chỉ pass ~20-25% từ IP dân dụng — đo 2026-09-07.
    # Vẫn bật vì fail nhanh (~1s) và khi pass thì tiết kiệm 1 credit Scrape.do.
    scrapedo_token: str | None = None
    fetch_direct_first: bool = True
    request_delay_seconds: float = 2.0
    html_cache_hours: int = 24

    # --- Gemini ---
    # KHÔNG dùng alias "gemini-flash-latest": nó trỏ model mới nhất, cũng là model
    # đông nhất, trả 503 "high demand" liên tục. Pin bản ổn định.
    gemini_api_key: str
    gemini_model: str = "gemini-3.7-flash"
    gemini_model_fallback: str = "gemini-flash-lite-latest"

    # --- Cloudflare R2 ---
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_public_base_url: str

    # --- Database ---
    # Neon Postgres. Local dev: postgres chạy docker, hoặc trỏ thẳng Neon.
    database_url: str
    db_pool_size: int = 5

    # --- Giới hạn ---
    max_links_per_batch: int = 20
    max_images_per_product: int = 12
    max_image_bytes: int = 12_000_000

    # Render free chỉ có 0.1 CPU / 512MB -> hạ 3 thông số này để không treo.
    # Local (Mac) cứ để 2000 / 4 / 4.
    image_max_edge: int = 2000
    image_concurrency: int = 4
    webp_method: int = 4          # 0 nhanh nhất, 6 nén tốt nhất
    port: int = 8787

    @property
    def pg_dsn(self) -> str:
        """asyncpg không hiểu prefix 'postgresql+asyncpg://' hay query 'sslmode'."""
        dsn = self.database_url.replace("postgresql+asyncpg://", "postgresql://")
        return dsn.replace("postgres://", "postgresql://", 1)

    @property
    def r2_endpoint(self) -> str:
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    @property
    def cdn_base(self) -> str:
        return self.r2_public_base_url.rstrip("/")


def _load() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = [".".join(str(p) for p in e["loc"]).upper() for e in exc.errors()]
        print("\n  Thiếu biến môi trường trong .env:\n", file=sys.stderr)
        for name in missing:
            print(f"    - {name}", file=sys.stderr)
        print("\n  Xem .env.example để biết lấy giá trị ở đâu.\n", file=sys.stderr)
        raise SystemExit(1) from None


settings = _load()
