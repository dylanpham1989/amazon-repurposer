"""Engine dán sticker. Scale/offset theo % ảnh nền, KHÔNG theo pixel —
ảnh Amazon có kích thước khác nhau, pixel cứng làm sticker khi to khi nhỏ bất nhất.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from PIL import Image
from pydantic import BaseModel, Field

from app.models import ProductData

Anchor = Literal[
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
]

ANCHORS: dict[str, tuple[float, float]] = {
    "top-left": (0.0, 0.0), "top-center": (0.5, 0.0), "top-right": (1.0, 0.0),
    "middle-left": (0.0, 0.5), "center": (0.5, 0.5), "middle-right": (1.0, 0.5),
    "bottom-left": (0.0, 1.0), "bottom-center": (0.5, 1.0), "bottom-right": (1.0, 1.0),
}


class Layer(BaseModel):
    asset_id: str
    anchor: Anchor = "top-right"
    scale: float = Field(0.16, ge=0.02, le=0.60)      # % chiều rộng ảnh nền
    offset_x: float = Field(0.03, ge=0.0, le=0.30)    # "cách mép X%"
    offset_y: float = Field(0.03, ge=0.0, le=0.30)
    opacity: float = Field(1.0, ge=0.1, le=1.0)
    rotation: float = Field(0.0, ge=-180, le=180)
    apply_to: Literal["all", "first_only"] = "all"
    when: Literal["always", "is_best_seller", "has_free_delivery"] = "always"


def resolve_offset(anchor: str, ox: float, oy: float) -> tuple[float, float]:
    """User nghĩ 'cách mép 3%'. Đảo dấu để trực giác đó đúng ở cả 4 góc."""
    if "right" in anchor:
        ox = -ox
    if "bottom" in anchor:
        oy = -oy
    return ox, oy


def applies(layer: Layer, product: ProductData, image_index: int) -> bool:
    """Thiếu dữ liệu -> KHÔNG dán. Badge sai sự thật nguy hiểm hơn thiếu badge."""
    if layer.apply_to == "first_only" and image_index != 0:
        return False
    if layer.when == "always":
        return True
    return bool(getattr(product, layer.when, False))


def apply_layer(base: Image.Image, sticker: Image.Image, layer: Layer) -> Image.Image:
    W, H = base.size

    target_w = max(1, int(W * layer.scale))
    ratio = target_w / sticker.width
    sticker = sticker.resize(
        (target_w, max(1, int(sticker.height * ratio))), Image.LANCZOS
    )

    if layer.rotation:
        sticker = sticker.rotate(layer.rotation, resample=Image.BICUBIC, expand=True)

    if sticker.mode != "RGBA":
        sticker = sticker.convert("RGBA")

    if layer.opacity < 1.0:
        alpha = sticker.getchannel("A").point(lambda p: int(p * layer.opacity))
        sticker.putalpha(alpha)

    ax, ay = ANCHORS[layer.anchor]
    ox, oy = resolve_offset(layer.anchor, layer.offset_x, layer.offset_y)
    sw, sh = sticker.size
    x = int(ax * W - ax * sw + ox * W)
    y = int(ay * H - ay * sh + oy * H)
    x = max(-sw // 2, min(x, W - sw // 2))
    y = max(-sh // 2, min(y, H - sh // 2))

    canvas = base.convert("RGBA")
    canvas.alpha_composite(sticker, (x, y))
    return canvas.convert("RGB")


def template_hash(layers: list[Layer]) -> str:
    """Canonicalize -> hash. Đổi template sinh R2 key mới, ảnh cũ vẫn còn."""
    payload = json.dumps(
        [layer.model_dump() for layer in layers], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()
