from app import auth
from app.config import settings


def test_correct_password():
    assert auth.check_password(settings.app_password)


def test_wrong_password():
    assert not auth.check_password(settings.app_password + "x")
    assert not auth.check_password("")


def test_cookie_roundtrip():
    class FakeReq:
        cookies = {auth.COOKIE_NAME: auth._signer.dumps("ok")}

    assert auth.is_logged_in(FakeReq())


def test_tampered_cookie_rejected():
    class FakeReq:
        cookies = {auth.COOKIE_NAME: "khong-phai-chu-ky-hop-le"}

    assert not auth.is_logged_in(FakeReq())


def test_no_cookie():
    class FakeReq:
        cookies = {}

    assert not auth.is_logged_in(FakeReq())


def test_csrf_roundtrip():
    assert auth.csrf_ok(auth.csrf_token())
    assert not auth.csrf_ok("rac")
    assert not auth.csrf_ok(None)
