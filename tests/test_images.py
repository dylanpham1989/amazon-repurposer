import io

import pytest
from PIL import Image

from app.services import images


def test_hires_strips_modifier_block():
    url = "https://m.media-amazon.com/images/I/71AbC._AC_SL1500_.jpg"
    c = images.hires_candidates(url)
    assert c[0] == "https://m.media-amazon.com/images/I/71AbC.jpg"   # ảnh gốc ưu tiên đầu
    assert c[-1] == url                                              # luôn có fallback
    assert len(set(c)) == len(c)


def test_hires_noop_when_no_modifier():
    url = "https://m.media-amazon.com/images/I/71AbC.jpg"
    assert images.hires_candidates(url) == [url]


def test_host_allowlist_blocks_ssrf():
    assert images.host_allowed("https://m.media-amazon.com/images/I/a.jpg")
    assert images.host_allowed("https://images-na.ssl-images-amazon.com/images/I/a.jpg")
    assert not images.host_allowed("http://127.0.0.1/a.jpg")
    assert not images.host_allowed("http://169.254.169.254/latest/meta-data/")
    assert not images.host_allowed("https://evil.com/images/I/a.jpg")


def _png(mode="RGB", size=(600, 600), color=(10, 20, 30)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_normalize_converts_cmyk_to_rgb():
    buf = io.BytesIO()
    Image.new("CMYK", (400, 400)).save(buf, format="JPEG")
    assert images.normalize(buf.getvalue()).mode == "RGB"


def test_normalize_flattens_alpha_onto_white():
    buf = io.BytesIO()
    Image.new("RGBA", (400, 400), (0, 0, 0, 0)).save(buf, format="PNG")
    out = images.normalize(buf.getvalue())
    assert out.mode == "RGB"
    assert out.getpixel((10, 10)) == (255, 255, 255)


def test_normalize_caps_long_edge():
    out = images.normalize(_png(size=(4000, 3000)))
    from app.config import settings
    assert max(out.size) == settings.image_max_edge


def test_normalize_rejects_tiny():
    with pytest.raises(images.ImageError):
        images.normalize(_png(size=(100, 100)))


def test_normalize_rejects_corrupt():
    with pytest.raises(images.ImageError):
        images.normalize(b"khong phai anh" * 200)


def test_sniff_rejects_unknown():
    assert images._sniff(b"\xff\xd8\xff") == "jpeg"
    assert images._sniff(b"\x89PNG\r\n\x1a\n") == "png"
    assert images._sniff(b"RIFF1234WEBP") == "webp"
    assert images._sniff(b"<html>") is None


def test_compose_outputs_webp():
    data, w, h = images.compose(_png(size=(800, 800)), [])
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert (w, h) == (800, 800)


def test_r2_key_is_content_addressed():
    k1 = images.r2_key("US", "B1", "abcd1234", 0, b"same")
    k2 = images.r2_key("US", "B1", "abcd1234", 0, b"same")
    k3 = images.r2_key("US", "B1", "abcd1234", 0, b"different")
    assert k1 == k2                     # chạy lại -> cùng key -> không upload trùng
    assert k1 != k3
    assert k1.startswith("p/US/B1/abcd1234/00-") and k1.endswith(".webp")


def test_r2_key_changes_with_template():
    a = images.r2_key("US", "B1", "aaaaaaaa", 0, b"x")
    b = images.r2_key("US", "B1", "bbbbbbbb", 0, b"x")
    assert a != b                       # đổi template -> key mới, ảnh cũ vẫn còn
