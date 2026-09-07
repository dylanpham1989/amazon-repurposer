# Reference: bản plan quy mô lớn (KHÔNG dùng cho MVP)

8 phase ở đây là bản thiết kế cho **2000 sản phẩm/ngày, nhiều user** — ARQ + Redis,
Postgres, adapter layer, circuit breaker, cost guard, Prometheus, Next.js riêng.
Ước tính 96h, chi phí $80–230/tháng.

User đã chốt hướng **đơn giản + free**. Bản đang dùng là 5 phase ở thư mục cha.

Giữ lại vì: khi nào vượt giới hạn free tier (Scrape.do 1000 req/tháng,
Gemini 1500 req/ngày, R2 10GB) thì đây là đường nâng cấp đã vẽ sẵn.
Đọc theo thứ tự phase-02 (adapter), phase-06 (queue), phase-08 (cost guard).
