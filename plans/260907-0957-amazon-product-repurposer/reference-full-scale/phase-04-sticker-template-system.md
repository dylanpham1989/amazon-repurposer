# Phase 4 — Sticker & Template System

**Priority:** P2 · **Effort:** 10h · **Status:** Pending
**Context:** [plan.md](./plan.md) · [Phase 3](./phase-03-image-pipeline.md)

## Overview

Cho user upload logo/sticker và định nghĩa template (sticker nào, ở đâu, to bao nhiêu, khi nào áp dụng) mà **không phải sửa code**. Phase 3 làm engine; phase này làm dữ liệu + API + logic điều kiện.

## Key Insights

- Yêu cầu gốc của user: "best seller góc trên bên phải, free delivery vào góc dưới bên phải **chẳng hạn**" — chữ "chẳng hạn" nghĩa là vị trí sẽ thay đổi. Hardcode là sai ngay từ đầu.
- **Conditional layer là thứ làm hệ thống hữu ích thật.** Dán "Best Seller" lên sản phẩm không phải best seller là quảng cáo sai sự thật. Điều kiện phải dựa trên dữ liệu scrape được (`is_best_seller`, `has_free_delivery`).
- Template phải **immutable khi đã dùng**. Sửa template đang được batch dùng → ảnh trong cùng batch không nhất quán. Giải pháp: versioning, batch tham chiếu snapshot.
- Text layer (không chỉ ảnh) rất hữu dụng: "-30%", "Chỉ còn 5" — nhưng font là hố sâu (Unicode, tiếng Việt có dấu). Cân nhắc để Phase sau nếu MVP gấp.

## Requirements

### Functional
- Upload sticker asset: PNG có alpha, ≤2MB, ≤1500×1500 → lưu R2 prefix `stickers/`
- CRUD template. Template = danh sách layer có thứ tự.
- Mỗi layer: asset, anchor (9 vị trí), scale, offset, opacity, rotation, `apply_to`, `condition`
- Điều kiện dựa trên `ProductData`: `is_best_seller`, `has_free_delivery`, `is_amazon_choice`, `price < X`, `rating >= X`, `always`
- Template mặc định của user, dùng khi batch không chỉ định
- Preview: render template lên ảnh mẫu, trả về ngay (không qua queue) — quan trọng cho UX
- Versioning: sửa template đang dùng → tạo version mới, batch cũ giữ version cũ

### Non-functional
- Preview trả về < 2s
- Validate sticker asset kỹ (đây là file user upload → đường tấn công)

## Architecture

### Layer schema (JSONB trong `sticker_templates.layers`)

```python
class LayerCondition(BaseModel):
    field: Literal["always", "is_best_seller", "has_free_delivery",
                   "is_amazon_choice", "price", "rating", "review_count"]
    op: Literal["is_true", "is_false", "lt", "lte", "gt", "gte", "eq"] = "is_true"
    value: float | None = None

class StickerLayer(BaseModel):
    id: str                                    # uuid, ổn định qua các lần sửa
    asset_id: UUID                             # -> sticker_assets.id
    anchor: Literal["top-left","top-center","top-right",
                    "middle-left","center","middle-right",
                    "bottom-left","bottom-center","bottom-right"]
    scale: float = Field(0.15, ge=0.02, le=0.60)      # % chiều rộng ảnh nền
    offset_x: float = Field(0.02, ge=-0.5, le=0.5)    # % chiều rộng, dương = sang phải
    offset_y: float = Field(0.02, ge=-0.5, le=0.5)    # % chiều cao,  dương = xuống dưới
    opacity: float = Field(1.0, ge=0.1, le=1.0)
    rotation: float = Field(0.0, ge=-180, le=180)
    apply_to: Literal["all", "first_only", "except_first"] = "all"
    condition: LayerCondition = LayerCondition(field="always")
    z_index: int = 0                           # thứ tự vẽ, nhỏ vẽ trước

class StickerTemplate(BaseModel):
    id: UUID
    name: str
    version: int
    is_default: bool
    layers: list[StickerLayer]
```

**Lưu ý về offset và anchor:** offset áp dụng theo hướng "vào trong" một cách trực giác không đúng cho mọi anchor. Ví dụ anchor `top-right` với `offset_x = 0.02` sẽ đẩy sticker **ra ngoài** cạnh phải. Giải pháp: engine tự đảo dấu offset theo anchor:

```python
def resolve_offset(anchor: str, ox: float, oy: float) -> tuple[float, float]:
    """Chuẩn hoá offset thành 'khoảng cách vào trong từ cạnh gần nhất'."""
    if "right" in anchor:  ox = -ox
    if "bottom" in anchor: oy = -oy
    return ox, oy
```
→ user luôn nghĩ "cách mép 2%", không cần biết dấu. Ghi rõ trong docstring.

### Template mặc định (seed lúc tạo user)

Khớp đúng ví dụ user đưa ra:
```json
{
  "name": "Default Badges",
  "layers": [
    {"asset": "best-seller.png",  "anchor": "top-right",    "scale": 0.20,
     "offset_x": 0.03, "offset_y": 0.03, "apply_to": "first_only",
     "condition": {"field": "is_best_seller", "op": "is_true"}},
    {"asset": "free-delivery.png","anchor": "bottom-right", "scale": 0.22,
     "offset_x": 0.03, "offset_y": 0.03, "apply_to": "all",
     "condition": {"field": "has_free_delivery", "op": "is_true"}}
  ]
}
```

### Đánh giá điều kiện

```python
def layer_applies(layer: StickerLayer, product: ProductData, image_index: int) -> bool:
    # 1. apply_to
    if layer.apply_to == "first_only" and image_index != 0: return False
    if layer.apply_to == "except_first" and image_index == 0: return False
    # 2. condition
    c = layer.condition
    if c.field == "always": return True
    val = getattr(product, c.field, None)
    if val is None: return False                # thiếu dữ liệu → KHÔNG dán (an toàn)
    match c.op:
        case "is_true":  return bool(val)
        case "is_false": return not bool(val)
        case "gt":  return float(val) >  c.value
        case "gte": return float(val) >= c.value
        case "lt":  return float(val) <  c.value
        case "lte": return float(val) <= c.value
        case "eq":  return float(val) == c.value
    return False
```

Nguyên tắc: **thiếu dữ liệu → không dán.** Dán badge sai nguy hiểm hơn thiếu badge.

### Versioning

Bảng bổ sung (migration Phase 4):
```sql
ALTER TABLE sticker_templates ADD COLUMN version INT NOT NULL DEFAULT 1;
ALTER TABLE sticker_templates ADD COLUMN parent_id UUID REFERENCES sticker_templates(id);
ALTER TABLE sticker_templates ADD COLUMN archived_at TIMESTAMPTZ;
CREATE INDEX ix_templates_user_active ON sticker_templates(user_id) WHERE archived_at IS NULL;
```

Khi PUT template đang được ≥1 batch **chưa completed** tham chiếu:
→ tạo row mới `version = old.version + 1`, `parent_id = old.id`; archive row cũ (`archived_at = now()`).
→ batch cũ vẫn trỏ `template_id` cũ. Không đụng.

Nếu chưa batch nào dùng → update tại chỗ (tránh sinh rác khi user đang chỉnh sửa).

### Preview endpoint (sync, không qua queue)

`POST /api/v1/templates/preview`
```json
{"template": {...layers...}, "sample": "default" | "<product_id>"}
```
→ render lên ảnh mẫu 1500×1500 (nền trắng có hình sản phẩm giả) hoặc ảnh thật của product đã có.
→ trả `image/webp` trực tiếp (không lưu R2 — preview là ephemeral).
→ Timeout cứng 5s, rate limit 20 req/phút/user.

## Related Code Files

**Create:**
- `apps/api/src/app/schemas/template.py`               — `StickerLayer`, `LayerCondition`, `StickerTemplate`
- `apps/api/src/app/services/templates/service.py`     — CRUD + versioning
- `apps/api/src/app/services/templates/conditions.py`  — `layer_applies()`
- `apps/api/src/app/services/templates/preview.py`     — render preview sync
- `apps/api/src/app/services/assets/upload.py`         — validate + upload sticker asset
- `apps/api/src/app/api/routes/templates.py`
- `apps/api/src/app/api/routes/assets.py`
- `apps/api/alembic/versions/0002_template_versioning.py`
- `apps/api/src/app/assets/sample-product.png`         — ảnh mẫu cho preview
- `apps/api/src/app/assets/seed/best-seller.png`
- `apps/api/src/app/assets/seed/free-delivery.png`
- `apps/api/tests/test_template_conditions.py`
- `apps/api/tests/test_template_versioning.py`

**Modify:**
- `apps/api/src/app/services/images/compositor.py` — nhận `StickerLayer`, gọi `resolve_offset()` + `layer_applies()`
- `apps/api/src/app/services/images/pipeline.py`   — nhận template + product, lọc layer theo điều kiện

## Implementation Steps

1. **Schema Pydantic** (`schemas/template.py`) với đầy đủ validator range. Đây là contract giữa API và engine.
2. **Migration `0002`** thêm versioning columns.
3. **Asset upload service:**
   - Chấp nhận **chỉ PNG** (yêu cầu alpha). Từ chối JPEG với message rõ ("sticker cần nền trong suốt, dùng PNG").
   - Magic bytes check, `Image.verify()`, ≤2MB, ≤1500×1500, phải có alpha channel (`img.mode in ("RGBA","LA","PA")`).
   - Upload R2 key `stickers/{user_id}/{sha256[:16]}.png`, `Cache-Control` dài.
   - Trả `sticker_assets` row với `cdn_url`, `width`, `height`.
4. **`conditions.py::layer_applies()`** + test bảng đầy đủ (mọi op × giá trị thiếu/có).
5. **`resolve_offset()`** trong compositor + test 4 góc.
6. **Template CRUD service** với versioning logic. Test: sửa template đang dùng → sinh version mới; sửa template chưa dùng → update tại chỗ.
7. **Preview renderer** — tái dùng compositor Phase 3, chạy trong ProcessPool, timeout cứng.
8. **Seed** — khi tạo user: upload 2 sticker mẫu + tạo template mặc định.
9. **API routes** — `GET/POST/PUT/DELETE /templates`, `POST /templates/preview`, `POST /assets`, `GET /assets`.

## Todo List

- [ ] `StickerLayer` + `LayerCondition` + `StickerTemplate` Pydantic có validator range
- [ ] Migration `0002`: `version`, `parent_id`, `archived_at` + partial index
- [ ] Asset upload: chỉ PNG, alpha bắt buộc, magic bytes, size cap, dimension cap
- [ ] Upload asset lên R2 prefix `stickers/`, key content-hashed
- [ ] `layer_applies()` + test bảng đầy đủ mọi op
- [ ] `resolve_offset()` chuẩn hoá offset theo anchor + test 4 góc
- [ ] Template CRUD + versioning (copy-on-write khi đang được dùng)
- [ ] Preview endpoint sync, timeout 5s, rate limit
- [ ] Ảnh mẫu `sample-product.png` cho preview
- [ ] Seed 2 sticker mẫu + template mặc định khi tạo user
- [ ] Wire compositor Phase 3 để nhận `StickerLayer` thật
- [ ] API routes templates + assets

## Success Criteria

- [ ] Upload PNG có alpha → thành công; upload JPEG → 400 với message rõ ràng
- [ ] Upload PNG **không có** alpha → 400 ("sticker cần nền trong suốt")
- [ ] Preview trả ảnh WebP < 2s, sticker đúng vị trí bằng mắt
- [ ] Anchor `top-right` + `offset 0.03` → sticker cách mép phải/trên đúng 3% (đo pixel trong test)
- [ ] Sản phẩm `is_best_seller=False` → layer Best Seller **không** được dán (assert bằng pixel)
- [ ] `apply_to="first_only"` → chỉ ảnh index 0 có sticker
- [ ] Sửa template đang được batch pending dùng → sinh `version=2`, batch cũ vẫn render bằng `version=1`
- [ ] Template hash (Phase 3) đổi khi layer đổi → sinh R2 key mới

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| Dán badge sai sự thật (Best Seller lên SP thường) | Trung bình | **Cao** (pháp lý/uy tín) | Điều kiện mặc định bám dữ liệu scrape; thiếu dữ liệu → không dán; UI cảnh báo khi user chọn `always` cho badge có tính khẳng định |
| User upload sticker độc hại (polyglot PNG/script) | Thấp | Cao | Magic bytes + `Image.verify()` + re-encode (mở rồi save lại → loại bỏ payload nhúng); serve từ domain riêng, `Content-Type` cố định |
| Sửa template giữa batch → ảnh không nhất quán | Trung bình | Trung bình | Copy-on-write versioning |
| Preview lạm dụng làm cạn CPU worker | Trung bình | Trung bình | Rate limit 20/phút/user, timeout 5s, ProcessPool riêng giới hạn 2 worker cho preview |
| Layer schema đổi sau này → template cũ vỡ | Trung bình | Trung bình | Thêm `schema_version` vào JSONB; chỉ thêm field optional, không xoá/đổi nghĩa |

## Security Considerations

- **File upload là đường tấn công chính của phase này.** Sau khi validate, **re-encode** ảnh (`Image.open()` → `save()`) để loại bỏ mọi payload nhúng trong chunk PNG.
- Sticker asset serve với `Content-Type: image/png` cứng, `X-Content-Type-Options: nosniff`.
- Template JSONB: validate qua Pydantic **trước** khi ghi DB. Không bao giờ trust JSONB đọc từ DB — validate lại khi load (dữ liệu có thể được ghi bởi version code cũ).
- Authorization: user chỉ đọc/sửa/xoá template và asset **của mình**. Kiểm tra `user_id` ở service layer, không chỉ ở route.
- R2 key sticker chứa `user_id` → không đoán được asset của user khác.

## Next Steps

→ [Phase 5 — AI Rewrite Engine](./phase-05-ai-rewrite-engine.md)
