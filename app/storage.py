"""Cloudflare R2 qua boto3 (S3-compatible). boto3 là sync -> chạy trong thread."""

from __future__ import annotations

import asyncio
import hashlib
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings

IMMUTABLE = "public, max-age=31536000, immutable"


@lru_cache(maxsize=1)
def client():
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def cdn_url(key: str) -> str:
    return f"{settings.cdn_base}/{key}"


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exists(key: str) -> bool:
    try:
        client().head_object(Bucket=settings.r2_bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _put(key: str, data: bytes, content_type: str, cache_control: str) -> bool:
    """Trả True nếu thật sự upload, False nếu object đã tồn tại."""
    # Key chứa hash nội dung -> object đã có nghĩa là nội dung y hệt.
    # Bỏ qua upload để tiết kiệm quota Class A (1 triệu/tháng free).
    if _exists(key):
        return False
    client().put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl=cache_control,
    )
    return True


async def put(
    key: str, data: bytes, content_type: str, cache_control: str = IMMUTABLE
) -> tuple[str, bool]:
    """Upload nếu chưa có. Trả (cdn_url, đã_upload_thật)."""
    uploaded = await asyncio.to_thread(_put, key, data, content_type, cache_control)
    return cdn_url(key), uploaded


async def delete_many(keys: list[str]) -> None:
    if not keys:
        return

    def _run() -> None:
        c = client()
        for i in range(0, len(keys), 1000):  # S3 giới hạn 1000 key/lần
            c.delete_objects(
                Bucket=settings.r2_bucket,
                Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]]},
            )

    await asyncio.to_thread(_run)


async def usage_bytes(prefix: str = "") -> int:
    """Tổng dung lượng đang dùng — để hiện 'X GB / 10 GB' trên trang quota."""

    def _run() -> int:
        total = 0
        paginator = client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.r2_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                total += obj["Size"]
        return total

    return await asyncio.to_thread(_run)


async def ping() -> bool:
    def _run() -> bool:
        client().head_bucket(Bucket=settings.r2_bucket)
        return True

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        return False
