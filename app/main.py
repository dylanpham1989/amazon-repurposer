"""FastAPI entrypoint — routes, lifespan, static, template."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import auth, db, storage
from app.config import settings
from app.services import jobs, quota

BASE = Path(__file__).parent
ASSET_V = "1"

templates = Jinja2Templates(directory=str(BASE / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    stuck = await db.recover_stuck_jobs()
    if stuck:
        print(f"  [startup] {stuck} sản phẩm mắc kẹt từ lần chạy trước -> đánh dấu failed")
    from app.services import templates as tsvc
    await tsvc.seed_default()
    app.state.render = render
    app.state.http = httpx.AsyncClient(
        http2=True, follow_redirects=True, timeout=httpx.Timeout(30.0, connect=8.0)
    )
    yield
    await app.state.http.aclose()
    await db.close()


app = FastAPI(title="Amazon Repurposer", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

from app.routes_extra import router as extra_router  # noqa: E402

app.include_router(extra_router)


def render(request: Request, name: str, ctx: dict[str, Any] | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request, name, {"asset_v": ASSET_V, "csrf": auth.csrf_token(), **(ctx or {})}
    )


def _secure_cookie(request: Request) -> bool:
    # Cookie Secure chỉ khi thực sự chạy HTTPS — nếu không, cookie bị trình duyệt
    # bỏ qua trên http://localhost và không đăng nhập được.
    return request.url.scheme == "https"


# --------------------------------------------------------------------- health
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def run_checks() -> list[dict[str, Any]]:
    """Kiểm tra từng dependency. Không raise — trả trạng thái để hiện lên UI."""

    async def check_db() -> tuple[bool, str]:
        try:
            ver = await db.fetch_one("SHOW server_version")
            n = await db.fetch_one("SELECT count(*) AS c FROM batches")
            v = str(ver[0]).split()[0] if ver else "?"
            return True, f"Postgres {v} · {n['c'] if n else 0} batch"
        except Exception as exc:
            return False, type(exc).__name__

    async def check_r2() -> tuple[bool, str]:
        ok = await storage.ping()
        return ok, f"bucket {settings.r2_bucket}" if ok else "không truy cập được"

    async def check_gemini() -> tuple[bool, str]:
        try:
            r = await app.state.http.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": settings.gemini_api_key, "pageSize": 1},
                timeout=12,
            )
            return r.status_code == 200, settings.gemini_model
        except Exception as exc:
            return False, type(exc).__name__

    async def check_scrapedo() -> tuple[bool, str]:
        if not settings.scrapedo_token:
            return False, "chưa cấu hình token"
        # Không gọi API thật ở đây — mỗi lần gọi tốn 1 trong 1000 credit/tháng.
        return True, "token đã cấu hình"

    names = ["Database", "Cloudflare R2", "Gemini", "Scrape.do"]
    results = await asyncio.gather(
        check_db(), check_r2(), check_gemini(), check_scrapedo(), return_exceptions=True
    )
    out = []
    for name, res in zip(names, results, strict=True):
        if isinstance(res, BaseException):
            out.append({"name": name, "ok": False, "detail": type(res).__name__})
        else:
            ok, detail = res
            out.append({"name": name, "ok": ok, "detail": detail})
    return out


@app.get("/readyz")
async def readyz() -> JSONResponse:
    checks = await run_checks()
    ok = all(c["ok"] for c in checks)
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


@app.get("/status", response_class=HTMLResponse)
async def status_partial(request: Request):
    if not auth.is_logged_in(request):
        return auth.redirect_to_login()
    return render(request, "_status.html", {"checks": await run_checks()})


# ----------------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(""), csrf: str = Form("")):
    if not auth.csrf_ok(csrf):
        return render(request, "login.html", {"error": "Phiên đã hết hạn, thử lại."})
    if not auth.check_password(password):
        return render(request, "login.html", {"error": "Mật khẩu không đúng."})
    resp = RedirectResponse("/", status_code=303)
    auth.issue(resp, secure=_secure_cookie(request))
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    auth.clear(resp)
    return resp


# ----------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not auth.is_logged_in(request):
        return auth.redirect_to_login()
    return render(
        request,
        "index.html",
        {
            "checks": await run_checks(),
            "max_links": settings.max_links_per_batch,
            "ready": True,
            "view": await _latest_view(request),
            "errors": [],
        },
    )


async def _latest_view(request: Request) -> dict | None:
    bid = request.query_params.get("batch")
    if not bid:
        row = await db.fetch_one("SELECT id FROM batches ORDER BY created_at DESC LIMIT 1")
        bid = row["id"] if row else None
    return await jobs.batch_view(bid) if bid else None


# --------------------------------------------------------------------- batch
@app.post("/submit", response_class=HTMLResponse)
async def submit(request: Request, urls: str = Form(""), csrf: str = Form("")):
    if not auth.is_logged_in(request):
        return auth.redirect_to_login()
    if not auth.csrf_ok(csrf):
        return RedirectResponse("/", status_code=303)

    batch_id, errors = await jobs.create_batch(urls.splitlines())
    if errors:
        return render(request, "index.html", {
            "checks": [], "max_links": settings.max_links_per_batch, "ready": True,
            "view": await _latest_view(request), "errors": errors, "urls": urls,
        })
    jobs.start(batch_id)
    return RedirectResponse(f"/?batch={batch_id}", status_code=303)


@app.get("/results/{batch_id}", response_class=HTMLResponse)
async def results(request: Request, batch_id: str):
    if not auth.is_logged_in(request):
        return auth.redirect_to_login()
    return render(request, "_results.html", {"view": await jobs.batch_view(batch_id)})


@app.post("/products/{product_id}/rewrite")
async def rewrite_one(request: Request, product_id: str, csrf: str = Form("")):
    if not auth.is_logged_in(request):
        return auth.redirect_to_login()
    if not auth.csrf_ok(csrf):
        return RedirectResponse("/", status_code=303)

    import json as _json

    import httpx as _httpx

    from app.services import rewrite as rw

    row = await db.fetch_one("SELECT * FROM products WHERE id=?", (product_id,))
    if row:
        data = jobs.product_data_from_row(dict(row))
        async with _httpx.AsyncClient(http2=True) as client:
            out = await rw.rewrite(data, client)
        await db.execute(
            "UPDATE products SET new_title=?, new_desc=?, rewrite_status=?, rewrite_flags=? "
            "WHERE id=?",
            (out.title, out.description, out.status,
             _json.dumps(out.flags, ensure_ascii=False), product_id),
        )
    return RedirectResponse(f"/?batch={row['batch_id']}" if row else "/", status_code=303)


@app.get("/batches/{batch_id}/export")
async def export(request: Request, batch_id: str, fmt: str = "json"):
    if not auth.is_logged_in(request):
        return auth.redirect_to_login()
    view = await jobs.batch_view(batch_id)
    if not view:
        return JSONResponse({"error": "not found"}, status_code=404)

    if fmt == "csv":
        import csv
        import io as _io

        maxi = max((len(p["images"]) for p in view["products"]), default=0)
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["asin", "url", "title_cu", "title_moi", "mota_cu", "mota_moi",
                    "rewrite", "nguon_mota", *[f"anh_{i+1}" for i in range(maxi)]])
        for p in view["products"]:
            w.writerow([p["asin"], p["source_url"], p["orig_title"], p["new_title"],
                        p["orig_desc"], p["new_desc"], p["rewrite_status"], p["desc_source"],
                        *(p["cdn_urls"] + [""] * (maxi - len(p["cdn_urls"])))])
        from fastapi.responses import Response
        return Response(
            buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
            headers={"content-disposition": f'attachment; filename="batch-{batch_id[:8]}.csv"'},
        )

    return JSONResponse(view)


@app.get("/quota", response_class=HTMLResponse)
async def quota_page(request: Request):
    if not auth.is_logged_in(request):
        return auth.redirect_to_login()
    used = await storage.usage_bytes()
    return render(request, "quota.html", {
        "quotas": await quota.snapshot(),
        "r2_gb": round(used / 1024 ** 3, 3),
        "r2_pct": min(100, round(used / (10 * 1024 ** 3) * 100)),
    })
