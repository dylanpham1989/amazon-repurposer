import io

import pytest
from PIL import Image

from app.services import templates as tsvc
from app.services.sticker import Layer


def png(mode="RGBA", size=(200, 200), color=(255, 0, 0, 128)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (255, 0, 0)).save(buf, format="JPEG")
    return buf.getvalue()


def test_rejects_jpeg():
    with pytest.raises(tsvc.AssetError, match="PNG"):
        tsvc.validate_and_clean(jpeg())


def test_rejects_png_without_alpha():
    """PNG đục sẽ dán một ô vuông đặc lên ảnh sản phẩm."""
    with pytest.raises(tsvc.AssetError, match="trong suốt"):
        tsvc.validate_and_clean(png(mode="RGB", color=(255, 0, 0)))


def test_rejects_fully_opaque_rgba():
    with pytest.raises(tsvc.AssetError, match="đục"):
        tsvc.validate_and_clean(png(color=(255, 0, 0, 255)))


def test_rejects_oversized_file():
    with pytest.raises(tsvc.AssetError, match="quá lớn"):
        tsvc.validate_and_clean(b"\x89PNG\r\n\x1a\n" + b"x" * 3_000_000)


def test_rejects_corrupt_png():
    with pytest.raises(tsvc.AssetError):
        tsvc.validate_and_clean(b"\x89PNG\r\n\x1a\n" + b"rac")


def test_accepts_valid_and_reencodes():
    clean, w, h = tsvc.validate_and_clean(png())
    assert clean.startswith(b"\x89PNG\r\n\x1a\n")
    assert (w, h) == (200, 200)
    # re-encode -> byte khác bản gốc (đã loại payload nhúng nếu có)
    assert Image.open(io.BytesIO(clean)).mode == "RGBA"


def test_downscales_huge_sticker():
    _, w, h = tsvc.validate_and_clean(png(size=(3000, 3000)))
    assert max(w, h) == tsvc.MAX_ASSET_DIM


def test_sample_image_is_neutral():
    img = tsvc.sample_image()
    assert img.size == tsvc.SAMPLE_SIZE
    assert img.getpixel((5, 5)) == (255, 255, 255)


def test_render_preview_applies_sticker():
    sticker = png(size=(100, 100), color=(0, 0, 255, 255))
    layers = [Layer(asset_id="s", anchor="top-right", scale=0.25, offset_x=0.02, offset_y=0.02)]
    out = tsvc.render_preview(layers, {"s": sticker})
    assert out[:4] == b"RIFF"
    img = Image.open(io.BytesIO(out)).convert("RGB")
    W, _ = img.size
    assert img.getpixel((W - 70, 70))[2] > 200      # xanh dương ở góc trên phải
    assert img.getpixel((70, 70)) == (255, 255, 255)  # góc trên trái vẫn trắng


def test_render_preview_respects_condition():
    """when=is_best_seller + sản phẩm mẫu không phải best seller -> KHÔNG dán."""
    sticker = png(size=(100, 100), color=(0, 0, 255, 255))
    layers = [Layer(asset_id="s", anchor="top-right", scale=0.25, when="is_best_seller")]
    out = tsvc.render_preview(layers, {"s": sticker}, best_seller=False)
    img = Image.open(io.BytesIO(out)).convert("RGB")
    W, _ = img.size
    assert img.getpixel((W - 70, 70)) == (255, 255, 255)


def test_render_preview_skips_missing_asset():
    out = tsvc.render_preview([Layer(asset_id="khong-ton-tai")], {})
    assert out[:4] == b"RIFF"      # không crash, chỉ bỏ qua lớp đó
