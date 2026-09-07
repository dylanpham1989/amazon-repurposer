# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx[http2]", "boto3"]
# ///
"""Kiểm tra toàn bộ env + ping 3 dịch vụ ngoài. KHÔNG in ra giá trị secret."""
import os, sys, json, pathlib, urllib.parse, time

import httpx, boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = pathlib.Path(__file__).resolve().parent.parent
OK, BAD, WARN = "\033[32m OK \033[0m", "\033[31mFAIL\033[0m", "\033[33mWARN\033[0m"
results = []


def load_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def report(name, ok, msg=""):
    tag = OK if ok is True else (WARN if ok == "warn" else BAD)
    print(f"[{tag}] {name}" + (f" — {msg}" if msg else ""))
    results.append(ok is True or ok == "warn")


E = load_env()
REQUIRED = ["APP_PASSWORD", "SESSION_SECRET", "SCRAPEDO_TOKEN", "GEMINI_API_KEY",
            "GEMINI_MODEL", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE_URL"]

print("\n=== 1. Biến môi trường ===")
missing = [k for k in REQUIRED if not E.get(k) or "DÁN VÀO ĐÂY" in E.get(k, "")]
if missing:
    report("env vars", False, f"chưa điền: {', '.join(missing)}")
else:
    report("env vars", True, f"{len(REQUIRED)}/{len(REQUIRED)} đã điền")

if E.get("R2_PUBLIC_BASE_URL", "").endswith("/"):
    report("R2_PUBLIC_BASE_URL", "warn", "có dấu / ở cuối — bỏ đi kẻo URL ảnh bị //")

# ---------------------------------------------------------------- Gemini
print("\n=== 2. Google Gemini ===")
gkey, gmodel = E.get("GEMINI_API_KEY", ""), E.get("GEMINI_MODEL", "gemini-flash-latest")
try:
    r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                  params={"key": gkey, "pageSize": 200}, timeout=20)
    if r.status_code != 200:
        report("API key", False, f"HTTP {r.status_code}: {r.text[:150]}")
    else:
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])]
        report("API key", True, f"{len(names)} model khả dụng")
        report(f"model '{gmodel}'", gmodel in names,
               "có trong danh sách" if gmodel in names
               else f"KHÔNG thấy. Flash có sẵn: {[n for n in names if 'flash' in n][:4]}")
except Exception as e:
    report("API key", False, repr(e))

# thử generate thật + structured output. 503 = model quá tải -> retry, rồi thử model khác.
body = {
    "contents": [{"parts": [{"text": "Rewrite slightly, keep all facts: 'Anker 20W USB-C charger, 2 ports.'"}]}],
    "generationConfig": {
        "responseMimeType": "application/json",
        "responseSchema": {"type": "OBJECT",
                           "properties": {"title": {"type": "STRING"}},
                           "required": ["title"]},
        "temperature": 0.4,
    },
}

def try_generate(model: str):
    return httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": gkey}, json=body, timeout=60)

done = False
for model in [gmodel, E.get("GEMINI_MODEL_FALLBACK", "gemini-flash-lite-latest"), "gemini-3.6-flash"]:
    for attempt in (1, 2, 3):
        try:
            r = try_generate(model)
        except Exception as e:
            report(f"generate ({model})", False, repr(e)); done = True; break
        if r.status_code == 200:
            out = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            note = f'trả về: "{out["title"][:55]}"'
            if model != gmodel:
                note += f"  [!] '{gmodel}' quá tải, '{model}' chạy được"
            report(f"generate + structured output", True, note)
            done = True; break
        if r.status_code == 503:
            time.sleep(4 * attempt); continue
        report(f"generate ({model})", False, f"HTTP {r.status_code}: {r.text[:180]}")
        done = True; break
    if done:
        break
else:
    report("generate", False, "503 ở cả 3 model sau nhiều lần thử — Google đang quá tải, thử lại sau")

# ---------------------------------------------------------------- R2
print("\n=== 3. Cloudflare R2 ===")
bucket = E.get("R2_BUCKET", "")
test_key = "_setup_check/hello.txt"
payload = b"amazon-repurposer setup check\n"
try:
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{E['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=E["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=E["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 2}),
    )
    s3.head_bucket(Bucket=bucket)
    report(f"bucket '{bucket}'", True, "truy cập được")

    s3.put_object(Bucket=bucket, Key=test_key, Body=payload,
                  ContentType="text/plain", CacheControl="no-store")
    report("upload (Class A)", True, f"{len(payload)} bytes")

    head = s3.head_object(Bucket=bucket, Key=test_key)
    report("head_object", head["ContentLength"] == len(payload), "idempotency check dùng được")

    base = E.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
    pr = httpx.get(f"{base}/{test_key}", timeout=20, follow_redirects=True)
    if pr.status_code == 200 and pr.content == payload:
        report("public URL", True, "đọc được qua CDN")
    else:
        report("public URL", False,
               f"HTTP {pr.status_code} — bucket chưa bật public access, "
               f"hoặc R2_PUBLIC_BASE_URL sai")

    s3.delete_object(Bucket=bucket, Key=test_key)
    report("cleanup", True, "đã xoá file test")
except ClientError as e:
    report("R2", False, f"{e.response['Error'].get('Code')}: {e.response['Error'].get('Message')}")
except Exception as e:
    report("R2", False, repr(e))

# ---------------------------------------------------------------- Amazon
print("\n=== 4. Lấy dữ liệu Amazon ===")
TEST_ASIN = "B0863TXGM3"   # Echo Dot (4th gen) — sản phẩm ổn định, luôn tồn tại
TEST_URL = f"https://www.amazon.com/dp/{TEST_ASIN}"
BLOCK_MARKERS = ["api-services-support@amazon.com", "Enter the characters you see below",
                 "To discuss automated access", "Type the characters you see in this image"]

def verdict(html: str) -> tuple[bool, str]:
    if len(html) < 5000:
        return False, f"trang quá ngắn ({len(html)} bytes) — gần như chắc chắn bị chặn"
    if any(m in html for m in BLOCK_MARKERS):
        return False, "trang CAPTCHA / 'Sorry' của Amazon"
    if "productTitle" not in html:
        return False, "không thấy #productTitle — không phải trang sản phẩm"
    return True, f"HTML hợp lệ ({len(html)//1024} KB)"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1", "Cache-Control": "max-age=0",
}

direct_ok = False
try:
    with httpx.Client(http2=True, follow_redirects=True, timeout=25) as c:
        r = c.get(TEST_URL, headers=HEADERS)
    good, why = verdict(r.text)
    direct_ok = good
    report("tầng 1 — fetch trực tiếp", good or "warn", f"HTTP {r.status_code} · {why}")
    if good:
        (ROOT / "data/fixtures/amazon_direct.html").write_text(r.text)
        print("        → đã lưu data/fixtures/amazon_direct.html (fixture cho Phase 2)")
except Exception as e:
    report("tầng 1 — fetch trực tiếp", "warn", repr(e))

try:  # tốn 1 credit của 1000/tháng
    r = httpx.get("https://api.scrape.do/",
                  params={"token": E.get("SCRAPEDO_TOKEN", ""), "url": TEST_URL},
                  timeout=90)
    if r.status_code == 401:
        report("tầng 2 — Scrape.do", False, "401 — token sai")
    elif r.status_code in (402, 429):
        report("tầng 2 — Scrape.do", False, f"HTTP {r.status_code} — hết quota hoặc rate limit")
    elif r.status_code != 200:
        report("tầng 2 — Scrape.do", False, f"HTTP {r.status_code}: {r.text[:200]}")
    else:
        good, why = verdict(r.text)
        report("tầng 2 — Scrape.do", good, why)
        rem = r.headers.get("Scrape-Do-Remaining-Credits") or r.headers.get("remaining-credits")
        if rem:
            print(f"        → credit còn lại: {rem}")
        if good:
            (ROOT / "data/fixtures/amazon_scrapedo.html").write_text(r.text)
            print("        → đã lưu data/fixtures/amazon_scrapedo.html")
except Exception as e:
    report("tầng 2 — Scrape.do", False, repr(e))

print("\n" + "=" * 60)
print(f"Kết quả: {sum(results)}/{len(results)} pass")
sys.exit(0 if all(results) else 1)
