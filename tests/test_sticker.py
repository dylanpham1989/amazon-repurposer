from PIL import Image

from app.models import ProductData
from app.services.sticker import Layer, applies, apply_layer, resolve_offset, template_hash


def base(size=(1000, 1000)):
    return Image.new("RGB", size, (255, 255, 255))


def sticker(size=(200, 200)):
    return Image.new("RGBA", size, (255, 0, 0, 255))


def test_resolve_offset_flips_on_right_and_bottom():
    assert resolve_offset("top-left", 0.03, 0.03) == (0.03, 0.03)
    assert resolve_offset("top-right", 0.03, 0.03) == (-0.03, 0.03)
    assert resolve_offset("bottom-left", 0.03, 0.03) == (0.03, -0.03)
    assert resolve_offset("bottom-right", 0.03, 0.03) == (-0.03, -0.03)


def _corner_colors(img):
    W, H = img.size
    return {
        "top-right": img.getpixel((W - 60, 60)),
        "top-left": img.getpixel((60, 60)),
        "bottom-right": img.getpixel((W - 60, H - 60)),
        "bottom-left": img.getpixel((60, H - 60)),
    }


def test_sticker_lands_in_requested_corner():
    for anchor in ("top-right", "top-left", "bottom-right", "bottom-left"):
        out = apply_layer(base(), sticker(), Layer(asset_id="a", anchor=anchor, scale=0.2))
        colors = _corner_colors(out)
        assert colors[anchor] == (255, 0, 0), f"{anchor} không có sticker"
        others = [c for k, c in colors.items() if k != anchor]
        assert all(c == (255, 255, 255) for c in others), f"{anchor} dán lem sang góc khác"


def test_scale_is_relative_to_width():
    """Ảnh khác kích thước -> sticker phải chiếm CÙNG tỉ lệ, không cùng pixel."""
    small = apply_layer(base((500, 500)), sticker(), Layer(asset_id="a", scale=0.2))
    large = apply_layer(base((2000, 2000)), sticker(), Layer(asset_id="a", scale=0.2))
    def red_ratio(img):
        px = img.load()
        W, H = img.size
        n = sum(1 for x in range(0, W, 5) for y in range(0, H, 5) if px[x, y] == (255, 0, 0))
        return n / ((W // 5) * (H // 5))
    assert abs(red_ratio(small) - red_ratio(large)) < 0.01


def test_applies_conditions():
    p = ProductData(asin="A", marketplace="US", is_best_seller=False, has_free_delivery=True)
    assert applies(Layer(asset_id="a", when="always"), p, 0)
    # thiếu/false -> KHÔNG dán. Badge sai sự thật nguy hiểm hơn thiếu badge.
    assert not applies(Layer(asset_id="a", when="is_best_seller"), p, 0)
    assert applies(Layer(asset_id="a", when="has_free_delivery"), p, 0)


def test_apply_to_first_only():
    lay = Layer(asset_id="a", apply_to="first_only")
    p = ProductData(asin="A", marketplace="US")
    assert applies(lay, p, 0)
    assert not applies(lay, p, 1)


def test_template_hash_is_key_order_independent():
    a = Layer(asset_id="x", anchor="top-right", scale=0.2)
    b = Layer(scale=0.2, anchor="top-right", asset_id="x")
    assert template_hash([a]) == template_hash([b])
    assert template_hash([a]) != template_hash([Layer(asset_id="x", anchor="top-left", scale=0.2)])
