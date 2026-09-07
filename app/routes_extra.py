"""Routes phần còn lại của Phase 5: template editor, asset, lịch sử, ZIP, sửa tay, xoá."""

from __future__ import annotations

import io
import zipfile

import httpx
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import auth, db, storage
from app.services import jobs
from app.services import templates as tsvc
from app.services.sticker import ANCHORS, Layer

router = APIRouter()
MAX_ZIP_IMAGES = 400
_FILE = File(...)   # B008: FastAPI cần call ở default, tách ra singleton


def _guard(request: Request):
    if not auth.is_logged_in(request):
        return auth.redirect_to_login()
    return None


# ------------------------------------------------------------------ lịch sử
@router.get("/batches", response_class=HTMLResponse)
async def batches_page(request: Request):
    if r := _guard(request):
        return r
    rows = await db.fetch_all(
        "SELECT b.*, (SELECT count(*) FROM images i JOIN products p ON p.id = i.product_id "
        "WHERE p.batch_id = b.id AND i.cdn_url IS NOT NULL) AS n_images "
        "FROM batches b ORDER BY b.created_at DESC LIMIT 100"
    )
    return request.app.state.render(
        request, "batches.html", {"batches": [jobs._row(r) for r in rows]}
    )


@router.post("/batches/{batch_id}/delete")
async def delete_batch(request: Request, batch_id: str, csrf: str = Form("")):
    if r := _guard(request):
        return r
    if not auth.csrf_ok(csrf):
        return RedirectResponse("/batches", status_code=303)
    # Xoá cả object trên R2 — không thì user tưởng đã xoá mà dữ liệu vẫn nằm đó.
    rows = await db.fetch_all(
        "SELECT i.r2_key FROM images i JOIN products p ON p.id = i.product_id "
        "WHERE p.batch_id = ? AND i.r2_key IS NOT NULL",
        (batch_id,),
    )
    await storage.delete_many([r["r2_key"] for r in rows])
    await db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    return RedirectResponse("/batches", status_code=303)


# ------------------------------------------------------------------ sửa tay
@router.post("/products/{product_id}/edit")
async def edit_product(
    request: Request, product_id: str,
    new_title: str = Form(""), new_desc: str = Form(""), csrf: str = Form(""),
):
    if r := _guard(request):
        return r
    row = await db.fetch_one("SELECT batch_id FROM products WHERE id=?", (product_id,))
    if auth.csrf_ok(csrf) and row:
        # Sửa tay được coi là 'ok': người thật đã duyệt nội dung.
        await db.execute(
            "UPDATE products SET new_title=?, new_desc=?, rewrite_status='ok', "
            "rewrite_flags='[\"đã sửa tay\"]' WHERE id=?",
            (new_title.strip(), new_desc.strip(), product_id),
        )
    return RedirectResponse(f"/?batch={row['batch_id']}" if row else "/", status_code=303)


# ------------------------------------------------------------------ ZIP
@router.get("/batches/{batch_id}/zip")
async def export_zip(request: Request, batch_id: str):
    if r := _guard(request):
        return r
    view = await jobs.batch_view(batch_id)
    if not view:
        return Response("not found", status_code=404)

    buf = io.BytesIO()
    n = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=40) as client:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # WebP đã nén rồi
            for p in view["products"]:
                for im in p["images"]:
                    if not im["cdn_url"] or n >= MAX_ZIP_IMAGES:
                        continue
                    try:
                        resp = await client.get(im["cdn_url"])
                        if resp.status_code == 200:
                            zf.writestr(
                                f"{p['asin'] or 'unknown'}/{im['position'] + 1:02d}.webp",
                                resp.content,
                            )
                            n += 1
                    except httpx.HTTPError:
                        continue
            zf.writestr("_info.txt", f"batch {batch_id}\n{n} ảnh\n")
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"content-disposition": f'attachment; filename="anh-{batch_id[:8]}.zip"'},
    )


# ------------------------------------------------------------------ sticker
@router.get("/templates", response_class=HTMLResponse)
async def templates_page(request: Request):
    if r := _guard(request):
        return r
    return request.app.state.render(request, "templates.html", {
        "templates": await tsvc.list_templates(),
        "assets": await tsvc.list_assets(),
        "anchors": list(ANCHORS),
        "error": request.query_params.get("err"),
    })


@router.post("/assets")
async def upload_asset(
    request: Request, name: str = Form(""), csrf: str = Form(""), file: UploadFile = _FILE
):
    if r := _guard(request):
        return r
    if not auth.csrf_ok(csrf):
        return RedirectResponse("/templates", status_code=303)
    try:
        await tsvc.save_asset(name or file.filename or "sticker", await file.read())
    except tsvc.AssetError as exc:
        return RedirectResponse(f"/templates?err={exc}", status_code=303)
    return RedirectResponse("/templates", status_code=303)


@router.post("/assets/{asset_id}/delete")
async def remove_asset(request: Request, asset_id: str, csrf: str = Form("")):
    if r := _guard(request):
        return r
    if auth.csrf_ok(csrf):
        await tsvc.delete_asset(asset_id)
    return RedirectResponse("/templates", status_code=303)


def _layers_from_form(form) -> list[Layer]:
    """Form gửi mảng song song: layer_asset[], layer_anchor[]... Ghép lại theo index."""
    assets = form.getlist("layer_asset")
    out: list[Layer] = []
    for i, aid in enumerate(assets):
        if not aid:
            continue

        def g(field: str, default: str, idx: int = i) -> str:
            vals = form.getlist(f"layer_{field}")
            return vals[idx] if idx < len(vals) else default

        out.append(Layer(
            asset_id=aid,
            anchor=g("anchor", "top-right"),           # type: ignore[arg-type]
            scale=float(g("scale", "0.16")) / 100,
            offset_x=float(g("ox", "3")) / 100,
            offset_y=float(g("oy", "3")) / 100,
            opacity=float(g("opacity", "100")) / 100,
            rotation=float(g("rotation", "0")),
            apply_to=g("apply_to", "all"),             # type: ignore[arg-type]
            when=g("when", "always"),                  # type: ignore[arg-type]
        ))
    return out


@router.post("/templates/{tid}")
async def save_template_route(request: Request, tid: str):
    if r := _guard(request):
        return r
    form = await request.form()
    if not auth.csrf_ok(str(form.get("csrf") or "")):
        return RedirectResponse("/templates", status_code=303)
    try:
        layers = _layers_from_form(form)
    except (ValueError, TypeError) as exc:
        return RedirectResponse(f"/templates?err=Giá trị không hợp lệ: {exc}", status_code=303)
    await tsvc.save_template(
        tid or db.new_id()[:12], str(form.get("name") or "Template"), layers, True
    )
    return RedirectResponse("/templates", status_code=303)


@router.post("/templates/preview/render")
async def preview(request: Request):
    """Render sync, trả ảnh luôn — không qua queue. Preview phải tức thì mới dùng được."""
    if not auth.is_logged_in(request):
        return Response(status_code=401)
    form = await request.form()
    try:
        layers = _layers_from_form(form)
    except (ValueError, TypeError):
        return Response(status_code=400)
    assets = {}
    for layer in layers:
        if (raw := await tsvc.load_asset_bytes(layer.asset_id)) is not None:
            assets[layer.asset_id] = raw
    import asyncio
    data = await asyncio.to_thread(tsvc.render_preview, layers, assets)
    return Response(data, media_type="image/webp", headers={"cache-control": "no-store"})
