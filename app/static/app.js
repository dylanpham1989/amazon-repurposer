// Toàn bộ JS của app. Cố ý giữ nhỏ.

// --- Nút copy -------------------------------------------------------------
// Event delegation trên document, KHÔNG gắn listener lên từng nút:
// HTMX swap nội dung mới vào liên tục, listener gắn trực tiếp sẽ chết theo.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;

  const text = btn.dataset.copy ?? "";
  let ok = true;
  try {
    // navigator.clipboard CHỈ chạy trên HTTPS hoặc localhost.
    // Mở qua IP LAN (192.168.x.x) sẽ rơi vào nhánh catch.
    await navigator.clipboard.writeText(text);
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;opacity:0;pointer-events:none";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      ta.remove();
    } catch {
      ok = false;
    }
  }

  const label = btn.dataset.label || btn.textContent;
  btn.dataset.label = label;
  btn.textContent = ok ? "đã copy" : "lỗi";
  btn.classList.toggle("done", ok);
  setTimeout(() => {
    btn.textContent = label;
    btn.classList.remove("done");
  }, 1300);
});

// Cảnh báo một lần nếu ngữ cảnh không cho phép clipboard API.
// Không có cảnh báo này thì user tưởng app hỏng.
if (!window.isSecureContext) {
  console.warn(
    "[copy] Không phải secure context — navigator.clipboard bị chặn. " +
      "Dùng http://localhost thay vì IP LAN, hoặc chạy qua HTTPS (cloudflared tunnel).",
  );
}

// --- Ctrl/Cmd + Enter để submit form đang focus ----------------------------
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    const form = e.target.closest("form");
    if (form) form.requestSubmit();
  }
});
