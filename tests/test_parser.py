"""Test parser chạy offline hoàn toàn — chỉ dùng fixture đã lưu."""
import pathlib

import pytest

from app.services import parser

FIXTURE = pathlib.Path("data/fixtures/amazon_scrapedo.html")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="chưa có fixture")


@pytest.fixture(scope="module")
def html():
    return FIXTURE.read_text()


@pytest.fixture(scope="module")
def product(html):
    return parser.parse_product(html, "B0863TXGM3", "US", max_images=20)


def test_not_blocked(html):
    assert not parser.looks_blocked(html)


def test_captcha_page_detected():
    captcha = "<html><body>Click the button below to continue shopping</body></html>"
    assert parser.looks_blocked(captcha)
    assert parser.looks_blocked("x" * 3781)
    assert parser.looks_blocked("<html>" + "y" * 100000 + "</html>")  # thiếu productTitle


def test_title(product):
    assert "Sony" in product.title and "WH-1000XM4" in product.title


def test_brand_uses_2026_selector(product):
    """#bylineInfo không tồn tại trên layout 2026 — phải lấy từ bảng Product Overview."""
    assert product.brand == "Sony"


def test_price(product):
    assert product.price and product.price.startswith("$")


def test_bullets(product):
    assert len(product.bullets) >= 5
    assert all("See more" not in b for b in product.bullets)


def test_description_source(product):
    assert product.desc_source == "product_description"
    assert len(product.description) > 50


def test_badges(product):
    assert product.is_best_seller is True
    assert product.has_free_delivery is True


def test_images_hires_and_deduped(product):
    assert len(product.images) >= 8
    assert len(set(product.images)) == len(product.images)
    assert all(u.startswith("https://") for u in product.images)
    # phải là ảnh lớn, không phải thumbnail US40
    assert sum("_SL1500_" in u or "_AC_" in u for u in product.images) >= 5


def test_only_current_variant_images(product):
    """Không được trộn ảnh của biến thể màu khác."""
    ids = {parser._img_id(u) for u in product.images}
    assert len(ids) == len(product.images)


def test_no_warnings(product):
    assert product.warnings == []


def test_description_falls_back_to_bullets():
    html = """<html><head></head><body><span id="productTitle">X</span>
    <div id="feature-bullets"><ul>
    <li><span class="a-list-item">Pin 30 gio lien tuc, sac nhanh 10 phut duoc 5 gio</span></li>
    <li><span class="a-list-item">Driver 40mm cho am thanh chi tiet va can bang</span></li>
    </ul></div>""" + "<!--" + "p" * 60000 + "-->" + "</body></html>"
    p = parser.parse_product(html, "X", "US")
    assert p.desc_source == "bullets"
    assert "30 gio" in p.description
