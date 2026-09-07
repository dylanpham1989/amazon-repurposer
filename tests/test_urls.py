import pytest

from app.services import urls


@pytest.mark.parametrize("raw,asin,mk", [
    ("https://www.amazon.com/dp/B0863TXGM3", "B0863TXGM3", "US"),
    ("https://amazon.com/dp/B0863TXGM3", "B0863TXGM3", "US"),
    ("www.amazon.com/dp/B0863TXGM3", "B0863TXGM3", "US"),
    ("amazon.com/dp/B0863TXGM3", "B0863TXGM3", "US"),
    ("https://www.amazon.com/Sony-WH1000XM4-Headphones/dp/B0863TXGM3/ref=sr_1_3?keywords=sony",
     "B0863TXGM3", "US"),
    ("https://www.amazon.com/gp/product/B0863TXGM3", "B0863TXGM3", "US"),
    ("https://www.amazon.com/gp/aw/d/B0863TXGM3", "B0863TXGM3", "US"),
    ("https://m.amazon.com/dp/B0863TXGM3", "B0863TXGM3", "US"),
    ("https://smile.amazon.com/dp/B0863TXGM3", "B0863TXGM3", "US"),
    ("https://www.amazon.de/dp/B0863TXGM3", "B0863TXGM3", "DE"),
    ("https://www.amazon.co.uk/dp/B0863TXGM3", "B0863TXGM3", "UK"),
    ("https://www.amazon.co.jp/dp/B0863TXGM3", "B0863TXGM3", "JP"),
    ("https://www.amazon.com.au/dp/B0863TXGM3", "B0863TXGM3", "AU"),
    ("https://www.amazon.de/-/en/dp/B0863TXGM3", "B0863TXGM3", "DE"),
    ("https://www.amazon.com/dp/B0863TXGM3?th=1&psc=1", "B0863TXGM3", "US"),
    ("https://www.amazon.com/product/B0863TXGM3", "B0863TXGM3", "US"),
    ("  https://www.amazon.com/dp/B0863TXGM3  ", "B0863TXGM3", "US"),
    ("B0863TXGM3", "B0863TXGM3", "US"),
    ("b0863txgm3", "B0863TXGM3", "US"),
])
def test_parse_ok(raw, asin, mk):
    p = urls.parse(raw)
    assert p.asin == asin
    assert p.marketplace == mk


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "https://google.com/dp/B0863TXGM3",
    "https://www.ebay.com/itm/12345",
    "https://www.amazon.com/",
    "https://www.amazon.com/s?k=headphones",
    "khong-phai-url",
    "https://www.amazon.com/gp/cart/view.html",
])
def test_parse_reject(raw):
    with pytest.raises(urls.InvalidUrlError):
        urls.parse(raw)


def test_shortlink_must_be_resolved_first():
    with pytest.raises(urls.InvalidUrlError, match="resolve_shortlink"):
        urls.parse("https://amzn.to/3abcdef")


def test_no_regex_scan_of_tracking_params():
    """Tracking param có chuỗi 10 ký tự — không được nhận nhầm thành ASIN."""
    with pytest.raises(urls.InvalidUrlError):
        urls.parse("https://www.amazon.com/s?ref=ABCDEFGHIJ&pd_rd_i=KLMNOPQRST")


def test_product_url():
    assert urls.product_url("B0863TXGM3", "amazon.de") == "https://www.amazon.de/dp/B0863TXGM3"
