# Phase 7 — Frontend Application

**Priority:** P1 · **Effort:** 14h · **Status:** Pending
**Context:** [plan.md](./plan.md) · [Phase 6](./phase-06-job-orchestration-api.md) · [Phase 4](./phase-04-sticker-template-system.md)

## Overview

Next.js 15 app: dán link → theo dõi tiến trình → xem/sửa kết quả → export. Cộng thêm trình sửa template sticker trực quan.

## Key Insights

- **Giá trị cốt lõi của UI nằm ở màn so sánh kết quả.** User cần thấy ảnh trước/sau và title/description gốc/mới cạnh nhau để tin tưởng output. Không có nó thì công cụ vô dụng — không ai đăng bán mà không kiểm tra.
- **Template editor phải có preview live.** Chỉnh số rồi chạy cả batch mới biết sai vị trí là trải nghiệm tệ và tốn tiền.
- Batch chạy tới **1 giờ** → user sẽ đóng tab. Progress phải khôi phục được khi quay lại, không phụ thuộc kết nối SSE liên tục.
- `rewrite_flags` phải hiển thị rõ. Sản phẩm `rewrite_status='failed'` mà UI im lặng → user đăng bán bản gốc mà tưởng đã rewrite.

## Requirements

### Functional
- Trang submit: textarea nhiều link (mỗi dòng 1), chọn template, chọn tone, chọn `source_authorization`
- Validate link client-side trước khi gửi (feedback tức thì)
- Danh sách batch có filter + phân trang
- Trang chi tiết batch: progress bar tổng + trạng thái từng sản phẩm, cập nhật realtime
- Trang chi tiết sản phẩm: gallery ảnh trước/sau, diff title/description gốc↔mới
- Sửa tay title/description, lưu lại
- Rewrite lại 1 sản phẩm
- Template editor: kéo/chọn anchor, chỉnh scale/offset/opacity, preview live
- Upload sticker asset
- Export JSON/CSV/ZIP
- Copy nhanh title/description/URL ảnh (thao tác dùng nhiều nhất)

### Non-functional
- Mobile responsive (user hay check kết quả trên điện thoại)
- Reconnect SSE tự động; fallback polling khi SSE fail
- Không chặn UI khi batch lớn (virtualize danh sách >100 item)

## Architecture

### Cấu trúc trang

```
apps/web/src/app/
├── (auth)/login/page.tsx
├── (app)/
│   ├── layout.tsx                  # sidebar + auth guard
│   ├── page.tsx                    # Submit — trang chính
│   ├── batches/
│   │   ├── page.tsx                # danh sách
│   │   └── [id]/
│   │       ├── page.tsx            # chi tiết + progress
│   │       └── products/[pid]/page.tsx
│   ├── templates/
│   │   ├── page.tsx
│   │   └── [id]/page.tsx           # editor + live preview
│   └── settings/page.tsx           # API key, quota, chi phí
```

### Component chính

| Component | Vai trò |
|-----------|---------|
| `LinkInput` | textarea, parse mỗi dòng, badge hợp lệ/lỗi realtime, đếm số link |
| `BatchProgress` | progress bar + breakdown theo stage (scraping/images/rewrite) |
| `ProductCard` | thumbnail + title mới + status badge + flag cảnh báo |
| `ImageCompare` | slider trước/sau, hoặc grid 2 cột |
| `TextDiff` | highlight khác biệt gốc↔mới (`diff-match-patch` hoặc `jsdiff`) |
| `TemplateEditor` | canvas 9 anchor + slider + preview debounced |
| `FlagBadge` | hiển thị `rewrite_flags` với tooltip giải thích |
| `ExportMenu` | JSON / CSV / ZIP |

### SSE hook

```ts
// hooks/useBatchProgress.ts
export function useBatchProgress(batchId: string) {
  const qc = useQueryClient()
  useEffect(() => {
    let es: EventSource | null = null
    let pollTimer: ReturnType<typeof setInterval> | null = null
    let retries = 0

    const startPolling = () => {
      pollTimer ??= setInterval(
        () => qc.invalidateQueries({ queryKey: ['batch', batchId] }), 3000)
    }

    const connect = () => {
      es = new EventSource(`${API}/batches/${batchId}/events`,
                           { withCredentials: true })
      es.onmessage = (e) => {
        retries = 0
        qc.setQueryData(['batch', batchId], JSON.parse(e.data))
      }
      es.addEventListener('done', () => { es?.close(); cleanup() })
      es.onerror = () => {
        es?.close()
        if (retries < 5) {
          setTimeout(connect, Math.min(1000 * 2 ** retries++, 15000))
        } else {
          startPolling()                     // fallback khi SSE hỏng hẳn
        }
      }
    }
    connect()
    return cleanup
  }, [batchId])
}
```

SSE + backoff reconnect + polling fallback. Cần cả ba — proxy/CDN có thể chặn SSE.

### Template Editor — preview live

```
User chỉnh slider
   → debounce 400ms
   → POST /templates/preview {template, sample}
   → nhận blob image/webp
   → <img src={URL.createObjectURL(blob)}>
   → revokeObjectURL ở cleanup (nếu không sẽ rò memory)
```

Anchor chọn bằng lưới 3×3 nút bấm — trực quan hơn dropdown và khớp trực tiếp với 9 giá trị `ANCHORS` ở backend.

Slider: `scale` 2–60%, `offset` 0–20% (hiển thị "cách mép X%"), `opacity` 10–100%, `rotation` −180…180°.

### Trang chi tiết sản phẩm — bố cục

```
┌─────────────────────────────────────────────────┐
│ [status badge] [rewrite flags] [nút Rewrite lại]│
├──────────────────────┬──────────────────────────┤
│ ẢNH GỐC (grid)       │ ẢNH ĐÃ XỬ LÝ (grid)      │
│                      │ + nút copy URL từng ảnh  │
├──────────────────────┴──────────────────────────┤
│ TITLE                                            │
│ gốc:  ...                          [copy]       │
│ mới:  ... (highlight diff)  [copy] [sửa]        │
├──────────────────────────────────────────────────┤
│ DESCRIPTION                                      │
│ gốc / mới song song, diff highlight              │
│ [copy] [sửa]                                     │
├──────────────────────────────────────────────────┤
│ Nguồn description: feature_bullets               │
│ Chi phí: scrape $0.0005 · LLM $0.0018            │
└──────────────────────────────────────────────────┘
```

Hiển thị `description_source` là quan trọng: user cần biết mô tả đến từ bullets hay từ product description thật.

## Related Code Files

**Create:**
- `apps/web/src/lib/api-client.ts`          — typed fetch + auth + error handling
- `apps/web/src/lib/types.ts`               — mirror Pydantic schema (hoặc sinh từ OpenAPI)
- `apps/web/src/lib/url-validate.ts`        — validate link Amazon client-side
- `apps/web/src/hooks/useBatchProgress.ts`
- `apps/web/src/hooks/useTemplatePreview.ts`
- `apps/web/src/components/LinkInput.tsx`
- `apps/web/src/components/BatchProgress.tsx`
- `apps/web/src/components/ProductCard.tsx`
- `apps/web/src/components/ImageCompare.tsx`
- `apps/web/src/components/TextDiff.tsx`
- `apps/web/src/components/FlagBadge.tsx`
- `apps/web/src/components/ExportMenu.tsx`
- `apps/web/src/components/template/TemplateEditor.tsx`
- `apps/web/src/components/template/AnchorGrid.tsx`
- `apps/web/src/components/template/LayerList.tsx`
- `apps/web/src/components/template/StickerUpload.tsx`
- các file `page.tsx` theo cấu trúc trên

## Implementation Steps

1. **Sinh type từ OpenAPI** — chạy `openapi-typescript` trên `/openapi.json` của FastAPI. Tránh drift type thủ công. Thêm vào script `pnpm gen:api`.
2. **`api-client.ts`** — wrapper fetch: base URL, credentials, refresh token khi 401, map error code sang message tiếng Việt.
3. **Auth flow** — login page, middleware bảo vệ route `(app)`, refresh tự động.
4. **`url-validate.ts`** — port logic parser từ backend (regex ASIN + domain list). Validate client-side để feedback tức thì; backend vẫn validate lại.
5. **`LinkInput`** — textarea, parse từng dòng, badge xanh/đỏ, hiện ASIN đã nhận diện, cảnh báo link trùng.
6. **Trang Submit** — form (react-hook-form + zod), chọn template, tone, `source_authorization` (kèm cảnh báo pháp lý khi chọn `unverified`).
7. **Danh sách batch** — TanStack Query, phân trang, filter status.
8. **`useBatchProgress`** + `BatchProgress` + trang chi tiết batch.
9. **`ProductCard`** grid, virtualize khi >100 item (`@tanstack/react-virtual`).
10. **Trang chi tiết sản phẩm** — `ImageCompare`, `TextDiff`, sửa tay, rewrite lại, copy buttons.
11. **`TemplateEditor`** — `AnchorGrid` 3×3, `LayerList` sắp xếp được, slider, preview debounce, revoke blob URL.
12. **`StickerUpload`** — drag & drop PNG, validate client-side, hiện lỗi rõ ràng.
13. **`ExportMenu`** — tải file, hiện tiến trình cho ZIP.
14. **Responsive + empty state + error boundary** cho mọi trang.

## Todo List

- [ ] Script `gen:api` sinh type từ OpenAPI
- [ ] `api-client.ts` + auto refresh 401 + map error code sang tiếng Việt
- [ ] Login page + middleware bảo vệ route
- [ ] `url-validate.ts` port từ backend parser
- [ ] `LinkInput` với badge hợp lệ/lỗi/trùng realtime
- [ ] Trang Submit + chọn template/tone/authorization + cảnh báo pháp lý
- [ ] Danh sách batch + filter + phân trang
- [ ] `useBatchProgress` SSE + backoff + polling fallback
- [ ] `BatchProgress` breakdown theo stage
- [ ] `ProductCard` grid + virtualize >100
- [ ] `ImageCompare` trước/sau
- [ ] `TextDiff` highlight khác biệt
- [ ] `FlagBadge` hiển thị `rewrite_flags` có tooltip
- [ ] Sửa tay title/description + lưu
- [ ] Nút rewrite lại 1 sản phẩm
- [ ] Copy button cho title/description/URL ảnh
- [ ] `AnchorGrid` 3×3
- [ ] `LayerList` sắp xếp lại thứ tự
- [ ] `TemplateEditor` slider + preview debounce + revoke blob URL
- [ ] `StickerUpload` drag & drop PNG
- [ ] `ExportMenu` 3 format
- [ ] Responsive + empty state + error boundary
- [ ] Trang settings: API key, quota, chi phí tháng

## Success Criteria

- [ ] Dán 10 link → thấy badge hợp lệ tức thì, link sai đỏ ngay, không cần gọi API
- [ ] Submit → chuyển sang trang batch, progress chạy realtime không cần refresh
- [ ] Tắt SSE (chặn endpoint) → UI tự chuyển polling, progress vẫn cập nhật
- [ ] Đóng tab giữa batch, mở lại → progress đúng trạng thái hiện tại
- [ ] Trang sản phẩm hiện ảnh trước/sau, sticker nhìn thấy rõ
- [ ] `rewrite_status='failed'` → badge cảnh báo nổi bật, tooltip giải thích lý do
- [ ] Template editor: đổi anchor → preview cập nhật < 1.5s
- [ ] Upload JPEG làm sticker → lỗi rõ ràng bằng tiếng Việt
- [ ] Batch 100 sản phẩm → danh sách cuộn mượt (virtualize hoạt động)
- [ ] Mobile 375px: mọi trang dùng được, không tràn ngang
- [ ] `tsc --noEmit` sạch, không dùng `any`

## Risk Assessment

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|--------|----------|----------|------------|
| SSE bị proxy/CDN chặn | Trung bình | Trung bình | Polling fallback bắt buộc; header `X-Accel-Buffering: no` |
| Type drift giữa FE và BE | Cao | Trung bình | Sinh type từ OpenAPI trong CI, fail build nếu lệch |
| Preview template spam API | Trung bình | Trung bình | Debounce 400ms + rate limit server (Phase 4) |
| Rò memory từ blob URL preview | Trung bình | Thấp | `URL.revokeObjectURL` ở cleanup effect |
| Batch lớn làm đơ trình duyệt | Trung bình | Trung bình | Virtualize danh sách; ảnh dùng `next/image` lazy load |
| User bỏ qua cảnh báo `rewrite_failed` → đăng bán sai | Trung bình | Cao | Badge nổi bật; hiện `rewrite_flags` chi tiết; export cũng có cột flag |

## Security Considerations

- Refresh token trong HttpOnly cookie, **không** localStorage. Access token giữ trong memory (React state), không persist.
- Sanitize mọi text từ API trước khi render — description gốc từ Amazon có thể chứa HTML/script. Dùng text node, **không** `dangerouslySetInnerHTML`. Nếu bắt buộc render HTML → DOMPurify.
- CSP header: `img-src` chỉ cho phép R2 CDN domain + `m.media-amazon.com`.
- Không log token/PII ra console ở production.
- Cảnh báo pháp lý ở trang Submit khi chọn `unverified` — bắt buộc tick xác nhận.

## Next Steps

→ [Phase 8 — Hardening & Deployment](./phase-08-hardening-deployment.md)
