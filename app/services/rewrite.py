"""Viết lại title + mô tả bằng Gemini.

Gộp CẢ HAI vào một lời gọi: free tier là 1.500 request/NGÀY, tách đôi thì chỉ được
750 sản phẩm. Gộp lại được 1.500 và nhanh gấp đôi.

Không dùng alias 'gemini-flash-latest' — nó trỏ model mới nhất, cũng là model đông
nhất, trả 503 "high demand" liên tục (đo 2026-09-07).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Literal

import httpx

from app.config import settings
from app.models import ProductData
from app.services import quota
from app.services.facts import check, facts_hint

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

MIN_DESC_CHARS = 40
MAX_DESC_CHARS = 12_000
MAX_TITLE_CHARS = 200
RPM_INTERVAL = 4.5          # free tier 15 RPM -> giữ khoảng cách >= 4s
MAX_ATTEMPTS = 2

TONES = {
    "neutral": "trung tính, rõ ràng, không hoa mỹ",
    "marketing": "hấp dẫn nhưng không phóng đại, không thêm tuyên bố mới",
    "concise": "ngắn gọn, đi thẳng vào thông tin",
}

SCHEMA = {
    "type": "OBJECT",
    "properties": {"title": {"type": "STRING"}, "description": {"type": "STRING"}},
    "required": ["title", "description"],
}

_last_call = 0.0
_lock = asyncio.Lock()


@dataclass
class RewriteOutcome:
    status: Literal["ok", "skipped", "failed"]
    title: str | None = None
    description: str | None = None
    flags: list[str] = field(default_factory=list)
    model_used: str | None = None
    attempts: int = 0


class RewriteError(Exception):
    pass


def build_prompt(p: ProductData, description: str, tone: str, hint: list[str] | None) -> str:
    fix = ""
    if hint:
        fix = (
            "\n\nLẦN TRƯỚC BẠN ĐÃ SAI: "
            + "; ".join(hint)
            + "\nSửa lại. Mọi con số và thương hiệu phải xuất hiện nguyên vẹn."
        )
    return f"""Bạn viết lại nội dung sản phẩm thương mại điện tử.

RÀNG BUỘC BẮT BUỘC — vi phạm là output không dùng được:
1. Giữ NGUYÊN mọi chi tiết sự kiện: con số, kích thước, đơn vị, mã model, tên thương
   hiệu, chất liệu, màu, số lượng, dung lượng, danh sách tương thích.
2. KHÔNG thêm thông tin không có trong bản gốc. Không bịa lợi ích, không bịa thông số,
   không dùng từ khẳng định ("tốt nhất", "số 1") nếu bản gốc không có.
3. KHÔNG bỏ chi tiết sự kiện nào có trong bản gốc.
4. Viết bằng ĐÚNG NGÔN NGỮ của bản gốc. Không dịch.
5. Chỉ đổi cách diễn đạt và cấu trúc câu — paraphrase nhẹ, không phải viết lại từ đầu.
6. Độ dài xấp xỉ bản gốc (±20%).
7. Title tối đa {MAX_TITLE_CHARS} ký tự, giữ thương hiệu và mã model ở đầu.
8. Plain text. Không markdown, không HTML, không emoji.

Giọng văn: {TONES.get(tone, TONES["neutral"])}

NHỮNG CHI TIẾT PHẢI XUẤT HIỆN NGUYÊN VĂN:
{facts_hint(p.title, description)}{fix}

=== TITLE GỐC ===
{p.title}

=== MÔ TẢ GỐC ===
{description}"""


async def _throttle() -> None:
    global _last_call
    async with _lock:
        wait = RPM_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


async def call_gemini(prompt: str, model: str, client: httpx.AsyncClient) -> dict:
    await _throttle()
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0.4,
        },
    }
    resp = await client.post(
        API.format(model=model),
        params={"key": settings.gemini_api_key},
        json=body,
        timeout=90,
    )
    if resp.status_code == 503:
        raise RewriteError("503")
    if resp.status_code == 429:
        raise RewriteError("429")
    if resp.status_code == 404:
        raise RewriteError("404")
    if resp.status_code != 200:
        raise RewriteError(f"HTTP {resp.status_code}: {resp.text[:160]}")

    await quota.bump("gemini")
    payload = resp.json()
    try:
        return json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RewriteError(f"output không parse được: {exc}") from exc


async def _call_with_fallback(prompt: str, client: httpx.AsyncClient) -> tuple[dict, str]:
    """503/404 -> thử model fallback. Model chính đông thì lite thường vẫn rảnh."""
    last: Exception | None = None
    for model in (settings.gemini_model, settings.gemini_model_fallback):
        for attempt in range(3):
            try:
                return await call_gemini(prompt, model, client), model
            except RewriteError as exc:
                last = exc
                if str(exc) in ("503", "429"):
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                break
    raise RewriteError(f"mọi model đều lỗi: {last}")


async def rewrite(
    p: ProductData, client: httpx.AsyncClient, *, tone: str = "neutral"
) -> RewriteOutcome:
    # Không có mô tả nguồn -> KHÔNG gọi LLM. Không cho bịa từ hư không.
    if p.desc_source == "none" or not p.description.strip():
        return RewriteOutcome("skipped", flags=["không có mô tả nguồn"])
    if len(p.description) < MIN_DESC_CHARS:
        return RewriteOutcome("skipped", flags=["mô tả nguồn quá ngắn"])

    description = p.description
    extra: list[str] = []
    if len(description) > MAX_DESC_CHARS:
        cut = description[:MAX_DESC_CHARS]
        description = cut[: cut.rfind(".") + 1] or cut
        extra.append("mô tả nguồn bị cắt bớt")

    hint: list[str] | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            out, model = await _call_with_fallback(
                build_prompt(p, description, tone, hint), client
            )
        except RewriteError as exc:
            return RewriteOutcome("failed", flags=[str(exc)], attempts=attempt)

        new_title = (out.get("title") or "").strip()[:MAX_TITLE_CHARS]
        new_desc = (out.get("description") or "").strip()

        errs = [f"title: {e}" for e in check(p.title, new_title, p.brand)]
        errs += [f"mô tả: {e}" for e in check(description, new_desc, None)]

        if not errs:
            return RewriteOutcome(
                "ok", new_title, new_desc, extra, model_used=model, attempts=attempt
            )
        hint = errs

    # Fail sau mọi lần thử -> GIỮ BẢN GỐC. An toàn hơn xuất bản nội dung sai.
    return RewriteOutcome("failed", flags=(hint or []) + extra, attempts=MAX_ATTEMPTS)
