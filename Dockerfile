# Render free: 512MB RAM / 0.1 CPU. Giữ image nhỏ, không layer thừa.
FROM python:3.12-slim

# Pillow cần lib hệ thống ở runtime. Buildpack tự động hay thiếu -> tự viết Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libjpeg62-turbo libwebp7 libpng16-16 zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Cài thẳng vào Python hệ thống (--system), KHÔNG dùng `uv sync`:
# uv sync tạo /app/.venv, còn CMD gọi uvicorn từ PATH -> không tìm thấy.
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY app ./app
COPY img ./img

RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8787
EXPOSE 8787

# 1 worker: job nền là asyncio.Task trong process — nhiều worker sẽ chạy trùng job.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8787} --workers 1"]
