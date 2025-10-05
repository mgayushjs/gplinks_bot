# web_bypass.py
"""
FastAPI web service to bypass shortlinks (gplinks.co, get2.in, etc.)
POST /bypass  -> JSON { "url": "...", "headless": true, "attempts": 3, "include_screenshot": true }
Optional API key: set env API_KEY; client must send header "x-api-key".
"""

import asyncio
import base64
import logging
import re
import time
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin, urlparse, unquote

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, AnyHttpUrl
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web-bypass")

app = FastAPI(title="GPLinks Bypass Service")

# === Config ===
DEFAULT_HEADLESS = True
NAV_TIMEOUT = 60_000
CLICK_TIMEOUT = 12_000
MAX_TOTAL_WAIT = 60
DEFAULT_ATTEMPTS = 3
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"

# optional API key
import os
API_KEY = os.environ.get("API_KEY")

# === Request / Response models ===
class BypassRequest(BaseModel):
    url: AnyHttpUrl
    headless: Optional[bool] = DEFAULT_HEADLESS
    attempts: Optional[int] = DEFAULT_ATTEMPTS
    include_screenshot: Optional[bool] = True

class BypassResponse(BaseModel):
    final_url: str
    raw_last_url: str
    captcha_detected: bool
    screenshot_b64: Optional[str] = None
    attempts_made: int

# === Helpers ===
def looks_final(u: Optional[str]) -> bool:
    if not u:
        return False
    u_l = u.lower()
    return ("gplinks.co" not in u_l) and ("gplinks" not in u_l)

def try_decode_get2_in(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        if "get2.in" not in parsed.netloc.lower():
            return None
        query = parsed.query or parsed.path.split("?", 1)[-1]
        if not query:
            return None
        candidate = query
        if "=" in query:
            parts = query.split("&")
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    if len(v) > 8:
                        candidate = v
                        break
        cand = unquote(candidate)
        try:
            b = cand.encode("utf-8")
            padding = (-len(b)) % 4
            if padding:
                b += b"=" * padding
            decoded = base64.urlsafe_b64decode(b).decode(errors="ignore")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass
        if cand.startswith("http"):
            return cand
    except Exception:
        pass
    return None

async def take_screenshot_b64(page: Page) -> str:
    b = await page.screenshot(full_page=True)
    return base64.b64encode(b).decode()

async def find_heroku_generate_in_page(page: Page) -> Optional[str]:
    """Scan page for Heroku /generate?code= links"""
    try:
        content = await page.content()
        matches = re.findall(r"https://[a-z0-9\-]+\.herokuapp\.com/generate\?code=[a-zA-Z0-9]+", content)
        if matches:
            return matches[0]
    except Exception:
        pass
    return None

# === Core bypass attempt ===
async def bypass_once(page: Page, url: str, attempt_num: int, progress_logger=None):
    result = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None}
    try:
        if progress_logger:
            progress_logger(f"[attempt {attempt_num}] goto {url}")
        try:
            await page.goto(url, timeout=NAV_TIMEOUT)
        except PlaywrightTimeoutError:
            logger.warning("page.goto timeout on attempt %s", attempt_num)
        except Exception:
            logger.exception("page.goto error attempt %s", attempt_num)

        try:
            btn = await page.wait_for_selector("a#btn-main, button#btn-main, a[role='button']", timeout=15000)
            if btn:
                if progress_logger:
                    progress_logger(f"[attempt {attempt_num}] clicking redirect button")
                try:
                    await btn.click(timeout=CLICK_TIMEOUT)
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass
        except Exception:
            pass

        start = time.time()
        last_url = page.url
        while time.time() - start < MAX_TOTAL_WAIT:
            current_url = page.url
            result["raw_last_url"] = current_url

            decoded = try_decode_get2_in(current_url)
            if decoded:
                result["final_url"] = decoded
                result["screenshot_b64"] = await take_screenshot_b64(page)
                return result

            heroku_target = await find_heroku_generate_in_page(page)
            if heroku_target:
                result["final_url"] = heroku_target
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                return result

            if looks_final(current_url) and current_url != url:
                result["final_url"] = current_url
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                return result

            try:
                content = await page.content()
            except Exception:
                content = ""
            if any(k in content.lower() for k in ("captcha", "recaptcha", "hcaptcha", "i am not a robot", "please verify")):
                result["captcha_detected"] = True
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                return result

            selectors = [
                "a#btn-main", "a[href*='redirect']", "a[href*='http']",
                "a.btn", "button#btn-main", "button", "input[type=submit]", "a[role='button']"
            ]
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        try:
                            await el.click(timeout=CLICK_TIMEOUT)
                            await page.wait_for_timeout(1000)
                        except Exception:
                            pass
                except Exception:
                    pass

            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass

            if page.url != last_url:
                last_url = page.url
            await page.wait_for_timeout(1000)

        result["final_url"] = page.url
        try:
            result["screenshot_b64"] = await take_screenshot_b64(page)
        except Exception:
            pass
        return result

    except Exception:
        logger.exception("Error in bypass_once")
        return result

# === Endpoint ===
@app.post("/bypass", response_model=BypassResponse)
async def bypass_endpoint(req: BypassRequest, x_api_key: Optional[str] = Header(None)):
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Missing/invalid API key")

    url = str(req.url)
    attempts = max(1, min(10, int(req.attempts or DEFAULT_ATTEMPTS)))
    headless = bool(req.headless)
    include_screenshot = bool(req.include_screenshot)

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Unsupported URL scheme")

    logger.info("Bypass request: url=%s attempts=%s headless=%s", url, attempts, headless)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        final = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None}
        attempt_made = 0
        try:
            for i in range(1, attempts + 1):
                attempt_made = i
                try:
                    await page.mouse.move(100, 100)
                except Exception:
                    pass

                res = await bypass_once(page, url, i, progress_logger=lambda t: logger.info("BYPASS: %s", t))
                final.update(res)
                if res.get("captcha_detected") or find_heroku_generate_in_page(page):
                    break

                if i < attempts:
                    backoff = (2 ** i) + 0.5 * i
                    logger.info("Waiting %.1fs before retry %d", backoff, i + 1)
                    await page.wait_for_timeout(int(backoff * 1000))
                    try:
                        await page.reload(timeout=5000)
                    except Exception:
                        pass
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

        b64 = final.get("screenshot_b64")
        if b64 and not include_screenshot:
            b64 = None

        return BypassResponse(
            final_url = final.get("final_url") or url,
            raw_last_url = final.get("raw_last_url") or url,
            captcha_detected = bool(final.get("captcha_detected")),
            screenshot_b64 = b64,
            attempts_made = attempt_made,
        )

@app.get("/health")
async def health():
    return {"status": "ok"}

# === Web UI ===

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GPLinks Bypass</title>
<style>
body { font-family: Arial, sans-serif; background: #f2f2f2; display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }
.container { background:#fff; padding:30px; border-radius:10px; box-shadow:0 0 15px rgba(0,0,0,0.1); max-width:400px; width:100%; }
input[type=text] { width:100%; padding:10px; margin:10px 0; border-radius:5px; border:1px solid #ccc; }
button { padding:10px 20px; background:#4CAF50; color:white; border:none; border-radius:5px; cursor:pointer; }
button:hover { background:#45a049; }
.result { margin-top:15px; word-break:break-all; }
</style>
</head>
<body>
<div class="container">
<h2>GPLinks Bypass</h2>
<input type="text" id="url" placeholder="Enter GPLinks URL">
<button onclick="startBypass()">Start</button>
<div class="result" id="result"></div>
</div>
<script>
async function startBypass() {
    const url = document.getElementById('url').value;
    if(!url) { alert('Please enter a URL'); return; }
    document.getElementById('result').innerText = '⏳ Bypassing...';
    try {
        const res = await fetch('/bypass', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({url:url, headless:true, include_screenshot:false, attempts:3})
        });
        const data = await res.json();
        if(data.final_url) {
            document.getElementById('result').innerHTML = `<strong>Final URL:</strong> <a href="${data.final_url}" target="_blank">${data.final_url}</a>`;
        } else {
            document.getElementById('result').innerText = '❌ Could not bypass URL';
        }
    } catch(e) {
        document.getElementById('result').innerText = '⚠️ Error: '+e;
    }
}
</script>
</body>
</html>
"""