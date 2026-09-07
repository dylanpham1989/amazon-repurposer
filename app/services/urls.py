"""Link Amazon -> (ASIN, marketplace). Nơi bug ẩn nhiều nhất -> test kỹ."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qs, urlparse

import httpx

from app.models import ParsedUrl

# Domain -> mã marketplace
AMAZON_DOMAINS: dict[str, str] = {
    "amazon.com": "US", "amazon.co.uk": "UK", "amazon.de": "DE", "amazon.fr": "FR",
    "amazon.it": "IT", "amazon.es": "ES", "amazon.co.jp": "JP", "amazon.ca": "CA",
    "amazon.com.au": "AU", "amazon.in": "IN", "amazon.com.mx": "MX", "amazon.com.br": "BR",
    "amazon.nl": "NL", "amazon.se": "SE", "amazon.pl": "PL", "amazon.sg": "SG",
    "amazon.ae": "AE", "amazon.sa": "SA", "amazon.com.tr": "TR", "amazon.com.be": "BE",
    "amazon.eg": "EG", "amazon.cn": "CN", "amazon.co.za": "ZA", "amazon.ie": "IE",
}
SHORTENERS = {"amzn.to", "amzn.eu", "a.co", "amzn.asia"}
DEFAULT_DOMAIN = "amazon.com"

ASIN_CHARS = re.compile(r"^[A-Z0-9]{10}$")
PATH_PATTERNS = [
    re.compile(p) for p in (
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/gp/aw/d/([A-Z0-9]{10})",
        r"/gp/offer-listing/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"/-/[a-z]{2}/dp/([A-Z0-9]{10})",
        r"/d/([A-Z0-9]{10})",
        r"/dp/product/([A-Z0-9]{10})",
    )
]
STRIP_SUBDOMAINS = ("www.", "smile.", "m.", "mobile.")


class InvalidUrlError(ValueError):
    pass


def _host_domain(host: str) -> str | None:
    """Bỏ subdomain, trả domain Amazon nếu khớp allowlist."""
    host = host.lower().split(":")[0]
    for pre in STRIP_SUBDOMAINS:
        if host.startswith(pre):
            host = host[len(pre) :]
    return host if host in AMAZON_DOMAINS else None


def _is_public_host(host: str) -> bool:
    """Chặn SSRF: không resolve tới IP nội bộ."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def _extract_asin(parsed) -> str | None:
    for pat in PATH_PATTERNS:
        if m := pat.search(parsed.path):
            return m.group(1)
    qs = parse_qs(parsed.query)
    for key in ("asin", "ASIN", "asins"):
        if key in qs and qs[key]:
            cand = qs[key][0].strip().upper()
            if ASIN_CHARS.match(cand):
                return cand
    return None
    # Cố ý KHÔNG quét regex ASIN trên toàn URL: dễ bắt nhầm chuỗi 10 ký tự
    # trong tracking param (ref=, pd_rd_i=...) -> ASIN sai mà không ai biết.


async def resolve_shortlink(url: str, client: httpx.AsyncClient) -> str:
    """Theo redirect của amzn.to / a.co. Chỉ chấp nhận đích là domain Amazon."""
    current = url
    for _ in range(3):
        parsed = urlparse(current)
        host = parsed.netloc.lower().split(":")[0]
        if _host_domain(host):
            return current
        if host not in SHORTENERS:
            raise InvalidUrlError(f"Link rút gọn trỏ tới domain lạ: {host}")
        if not _is_public_host(host):
            raise InvalidUrlError(f"Host phân giải ra IP nội bộ: {host}")
        resp = await client.get(current, follow_redirects=False, timeout=6)
        loc = resp.headers.get("location")
        if not loc:
            raise InvalidUrlError("Link rút gọn không trả về redirect")
        current = loc
    raise InvalidUrlError("Quá 3 lần chuyển hướng")


def parse(url: str, *, default_domain: str = DEFAULT_DOMAIN) -> ParsedUrl:
    """Parse đồng bộ. Link rút gọn phải resolve_shortlink() trước."""
    raw = url.strip().strip("<>​‎‏")
    if not raw:
        raise InvalidUrlError("Link rỗng")

    # ASIN thô, không phải URL
    bare = raw.upper()
    if ASIN_CHARS.match(bare) and not raw.lower().startswith(("http", "www.")):
        return ParsedUrl(asin=bare, marketplace=AMAZON_DOMAINS[default_domain],
                         domain=default_domain, original=raw)

    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = parsed.netloc.lower().split(":")[0]
    if host in SHORTENERS:
        raise InvalidUrlError(f"Link rút gọn ({host}) — phải resolve_shortlink() trước")

    domain = _host_domain(host)
    if domain is None:
        raise InvalidUrlError(f"Không phải link Amazon: {host or raw[:60]}")

    asin = _extract_asin(parsed)
    if asin is None:
        raise InvalidUrlError("Không tìm thấy ASIN trong link (cần dạng /dp/XXXXXXXXXX)")

    return ParsedUrl(asin=asin, marketplace=AMAZON_DOMAINS[domain],
                     domain=domain, original=url.strip())


def product_url(asin: str, domain: str) -> str:
    return f"https://www.{domain}/dp/{asin}"
