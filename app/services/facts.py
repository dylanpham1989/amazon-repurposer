"""Trích số liệu và kiểm tra bản viết lại có giữ đúng nội dung không.

LLM sẽ bịa số — không phải "có thể" mà là vài % chắc chắn. Đăng bán sai kích thước
= khách trả hàng. Đây là ~40 dòng chặn được hại thật.
"""

from __future__ import annotations

import re

NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
UNIT_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*"
    r"(mm|cm|m|in|inch|inches|ft|kg|g|lb|lbs|oz|ml|l|liter|litre|w|kw|v|mah|ah|"
    r"hz|khz|mhz|ghz|gb|tb|mb|kb|bit|mp|fps|rpm|psi|db|%|pcs|pack|count|ct)\b",
    re.I,
)

# Số nhỏ trong văn xuôi ("2 ports", "one of 3 modes") đổi cách diễn đạt là bình thường.
# Chỉ bắt lỗi với số có ý nghĩa thông số.
TRIVIAL = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "100"}


def normalize_numbers(text: str) -> set[str]:
    """'1,299.50' và '1299.5' phải là một. Xử lý cả dấu phẩy thập phân châu Âu."""
    out: set[str] = set()
    for raw in NUM_RE.findall(text):
        s = raw
        if "," in s and "." in s:
            # 1,299.50 (Anh-Mỹ) vs 1.299,50 (châu Âu) — dấu nào đứng sau là dấu thập phân
            if s.rfind(".") > s.rfind(","):
                s = s.replace(",", "")
            else:
                s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            parts = s.split(",")
            # ",50" -> thập phân;  ",299" hoặc nhiều nhóm -> ngăn cách hàng nghìn
            if len(parts) == 2 and len(parts[1]) != 3:
                s = s.replace(",", ".")
            else:
                s = s.replace(",", "")
        try:
            out.add(f"{float(s):g}")
        except ValueError:
            continue
    return out


def significant_numbers(text: str) -> set[str]:
    return {n for n in normalize_numbers(text) if n not in TRIVIAL}


def units(text: str) -> set[str]:
    return {u.lower() for u in UNIT_RE.findall(text)}


def check(orig: str, new: str, brand: str | None = None) -> list[str]:
    """Trả list lỗi. Rỗng = pass."""
    errs: list[str] = []
    if not new.strip():
        return ["output rỗng"]

    a, b = significant_numbers(orig), significant_numbers(new)
    if missing := sorted(a - b):
        errs.append(f"mất số: {', '.join(missing[:5])}")
    if added := sorted(b - a):
        errs.append(f"bịa số: {', '.join(added[:5])}")   # lỗi nghiêm trọng nhất
    if brand and brand.strip() and brand.lower() not in new.lower():
        errs.append(f"mất thương hiệu '{brand}'")
    if new.strip() == orig.strip():
        errs.append("không đổi gì so với bản gốc")
    ratio = len(new) / max(1, len(orig))
    if not 0.55 <= ratio <= 1.7:
        errs.append(f"độ dài lệch quá nhiều ({ratio:.0%} so với gốc)")
    return errs


def facts_hint(title: str, description: str) -> str:
    """Trích sẵn fact đưa vào prompt — tăng tỉ lệ giữ đúng hơn hẳn so với chỉ dặn dò."""
    text = f"{title}\n{description}"
    nums = sorted(significant_numbers(text), key=lambda x: -len(x))[:25]
    us = sorted(units(text))[:12]
    parts = []
    if nums:
        parts.append("Số phải giữ nguyên: " + ", ".join(nums))
    if us:
        parts.append("Đơn vị xuất hiện: " + ", ".join(us))
    return "\n".join(parts) or "(không có số liệu cụ thể)"
