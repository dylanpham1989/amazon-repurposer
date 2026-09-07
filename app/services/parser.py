"""HTML Amazon -> ProductData. Đây là cái giá của phương án free: tự viết parser.

Mọi selector đều có fallback và trả None thay vì raise — mất 1 field còn hơn mất cả
sản phẩm. Test bằng fixture đã lưu, chạy offline (tests/test_parser.py).
"""

from __future__ import annotations

import html as html_mod
import json
import re

from selectolax.parser import HTMLParser

from app.models import ProductData

# Amazon trả HTTP 200 kèm trang CAPTCHA -> không thể dựa vào status code.
BLOCK_MARKERS = (
    "api-services-support@amazon.com",
    "Enter the characters you see below",
    "To discuss automated access",
    "Type the characters you see in this image",
    "Click the button below to continue shopping",
)
MIN_HTML_BYTES = 50_000  # trang sản phẩm thật luôn > 500KB; trang chặn ~2-4KB


def looks_blocked(html: str) -> bool:
    if len(html) < MIN_HTML_BYTES:
        return True
    if any(m in html for m in BLOCK_MARKERS):
        return True
    return "productTitle" not in html


def _text(tree: HTMLParser, *selectors: str) -> str | None:
    for sel in selectors:
        node = tree.css_first(sel)
        if node:
            t = node.text(separator=" ", strip=True)
            if t:
                return re.sub(r"\s+", " ", t)
    return None


# ------------------------------------------------------------------ ảnh
IMG_ID = re.compile(r"/images/I/([A-Za-z0-9%2B_+-]+?)(?:\._[^.]*)?\.(?:jpg|png|webp|gif)", re.I)
# 'colorImages': { 'initial': A.$.parseJSON('[...]')  -- ảnh của ĐÚNG ASIN đang hỏi
INITIAL_RE = re.compile(
    r"['\"]colorImages['\"]\s*:\s*\{\s*['\"]initial['\"]\s*:\s*"
    r"(?:A\.\$\.parseJSON\(\s*)?['\"](\[.*?\])['\"]",
    re.S,
)
LANDING_COLOR_RE = re.compile(r'"landingAsinColor"\s*:\s*"([^"]*)"')
ALL_COLORS_RE = re.compile(r'"colorImages"\s*:\s*(\{.*?\}\]\})', re.S)


def _img_id(url: str) -> str | None:
    m = IMG_ID.search(url)
    return m.group(1) if m else None


def _unescape_js_string(s: str) -> str:
    """Chuỗi JSON nằm trong string JS -> gỡ escape trước khi json.loads."""
    return (
        s.replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
        .replace("\\/", "/")
        .replace("\\n", "")
    )


def _pick(entry: dict) -> str | None:
    for key in ("hiRes", "large", "thumb"):
        v = entry.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def extract_images(html: str, tree: HTMLParser, limit: int) -> list[str]:
    """3 tầng fallback. Dedupe theo image ID (cùng ảnh có nhiều size khác nhau)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(url: str | None) -> None:
        if not url or not url.startswith("http"):
            return
        iid = _img_id(url)
        key = iid or url
        if key in seen:
            return
        seen.add(key)
        out.append(url)

    # 1. 'initial' — ảnh của đúng ASIN đang hỏi. Nguồn tốt nhất.
    if m := INITIAL_RE.search(html):
        try:
            for e in json.loads(_unescape_js_string(m.group(1))):
                if isinstance(e, dict):
                    add(_pick(e))
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. "colorImages" của biến thể đang xem (theo landingAsinColor).
    #    KHÔNG gộp mọi màu — ảnh màu khác không thuộc ASIN này.
    if not out and (mc := ALL_COLORS_RE.search(html)):
        try:
            colors = json.loads(mc.group(1))
            lm = LANDING_COLOR_RE.search(html)
            key = lm.group(1) if lm and lm.group(1) in colors else next(iter(colors), None)
            for e in colors.get(key) or []:
                if isinstance(e, dict):
                    add(_pick(e))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    # 3. attribute data-a-dynamic-image trên thẻ img — lấy URL có kích thước lớn nhất.
    if not out:
        for node in tree.css("[data-a-dynamic-image]"):
            raw = node.attributes.get("data-a-dynamic-image") or ""
            try:
                mapping = json.loads(html_mod.unescape(raw))
            except json.JSONDecodeError:
                continue
            best, best_px = None, -1
            for url, dims in mapping.items():
                px = 0
                if isinstance(dims, list) and len(dims) == 2:
                    try:
                        px = int(dims[0]) * int(dims[1])
                    except (TypeError, ValueError):
                        px = 0
                if px > best_px:
                    best, best_px = url, px
            add(best)

    # 4. cuối cùng: thumbnail trong dải ảnh phụ (Phase 3 sẽ nâng hi-res)
    if not out:
        for node in tree.css("#altImages img, #imageBlock img, #landingImage"):
            add(node.attributes.get("src"))

    return out[:limit]


# ------------------------------------------------------------------ badge
def _is_best_seller(html: str, tree: HTMLParser) -> bool:
    if tree.css_first("#zeitgeistBadge_feature_div") or tree.css_first("#acBadge_feature_div"):
        return True
    return bool(re.search(r"#\d+\s+Best\s+Seller|\bBest\s?Seller\b", html))


def _has_free_delivery(html: str, tree: HTMLParser) -> bool:
    block = _text(tree, "#deliveryBlockMessage", "#mir-layout-DELIVERY_BLOCK") or ""
    hay = block or html
    return bool(re.search(r"FREE\s+(delivery|Delivery|shipping|Shipping)", hay))


# ------------------------------------------------------------------ mô tả
def _bullets(tree: HTMLParser) -> list[str]:
    out = []
    for node in tree.css("#feature-bullets ul li span.a-list-item, #feature-bullets ul li"):
        t = re.sub(r"\s+", " ", node.text(separator=" ", strip=True))
        if t and len(t) > 2 and "See more" not in t and t not in out:
            out.append(t)
    return out


def _aplus_text(tree: HTMLParser) -> str:
    parts = []
    for root in ("#aplus", "#aplus_feature_div", "#productDescription_feature_div"):
        node = tree.css_first(root)
        if node:
            parts.append(re.sub(r"\s+", " ", node.text(separator=" ", strip=True)))
    return " ".join(p for p in parts if p).strip()


def _description(tree: HTMLParser, bullets: list[str]) -> tuple[str, str]:
    """Fallback chain 4 bước. Trả (text, nguồn)."""
    pd = _text(tree, "#productDescription", "#bookDescription_feature_div")
    if pd and len(pd) >= 50:
        return pd, "product_description"
    joined = " ".join(bullets).strip()
    if len(joined) >= 50:
        return joined, "bullets"
    ap = _aplus_text(tree)
    if len(ap) >= 50:
        return ap, "aplus"
    return "", "none"


# ------------------------------------------------------------------ entry
def parse_product(html: str, asin: str, marketplace: str, *, max_images: int = 12) -> ProductData:
    tree = HTMLParser(html)
    warnings: list[str] = []

    title = _text(tree, "#productTitle", "#title", "h1#title span") or ""
    if not title:
        warnings.append("title_missing")

    bullets = _bullets(tree)
    description, desc_source = _description(tree, bullets)
    if desc_source == "none":
        warnings.append("description_missing")

    # #bylineInfo KHÔNG tồn tại trên layout 2026 — đã kiểm tra trên fixture thật.
    brand = _text(
        tree,
        "tr.po-brand td.a-span9 span",
        "#productOverview_feature_div tr:nth-child(1) td:nth-child(2) span",
        "#bylineInfo",
        "a#bylineInfo",
    )
    if brand:
        brand = re.sub(r"^(Visit the|Brand:|Thương hiệu:)\s*", "", brand).strip()
        brand = re.sub(r"\s*Store$", "", brand).strip()

    images = extract_images(html, tree, max_images)
    if not images:
        warnings.append("no_images")

    return ProductData(
        asin=asin,
        marketplace=marketplace,
        title=title,
        description=description,
        desc_source=desc_source,  # type: ignore[arg-type]
        bullets=bullets,
        images=images,
        brand=brand,
        price=_text(tree, ".a-price .a-offscreen", "#priceblock_ourprice", "#price"),
        is_best_seller=_is_best_seller(html, tree),
        has_free_delivery=_has_free_delivery(html, tree),
        warnings=warnings,
    )
