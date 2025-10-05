# web_bypass.py
"""
FastAPI web service with interactive web UI to bypass shortlinks (gplinks.co, get2.in, etc.)
POST /bypass  -> JSON { "url": "...", "headless": true, "attempts": 3, "include_screenshot": true }
Optional API key: set env API_KEY; client must send header "x-api-key".
"""

import asyncio
import base64
import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse, unquote

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
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

API_KEY = None
import os
API_KEY = os.environ.get("API_KEY")

# === Models ===
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
    return bool(u) and ("gplinks.co" not in u.lower()) and ("gplinks" not in u.lower())

def try_decode_get2_in(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        if "get2.in" not in parsed.netloc.lower(): return None
        query = parsed.query or parsed.path.split("?", 1)[-1]
        if not query: return None
        candidate = query.split("&")[-1].split("=",1)[-1]
        cand = unquote(candidate)
        try:
            b = cand.encode("utf-8")
            padding = (-len(b)) % 4
            if padding: b += b"=" * padding
            decoded = base64.urlsafe_b64decode(b).decode(errors="ignore")
            if decoded.startswith("http"): return decoded
        except Exception:
            pass
        if cand.startswith("http"): return cand
    except Exception:
        pass
    return None

async def take_screenshot_b64(page: Page) -> str:
    b = await page.screenshot(full_page=True)
    return base64.b64encode(b).decode()

async def bypass_once(page: Page, url: str, attempt_num: int, progress_logger=None):
    result = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None}
    try:
        if progress_logger: progress_logger(f"[attempt {attempt_num}] goto {url}")
        try: await page.goto(url, timeout=NAV_TIMEOUT)
        except Exception: pass

        # early button click
        try:
            btn = await page.wait_for_selector("a#btn-main, button#btn-main, a[role='button']", timeout=15000)
            if btn:
                try:
                    if progress_logger: progress_logger(f"[attempt {attempt_num}] clicking redirect button")
                    await btn.click(timeout=CLICK_TIMEOUT)
                    await page.wait_for_timeout(1500)
                except Exception: pass
        except Exception: pass

        start = time.time()
        last_url = page.url
        while time.time() - start < MAX_TOTAL_WAIT:
            current_url = page.url
            result["raw_last_url"] = current_url

            decoded = try_decode_get2_in(current_url)
            if decoded:
                result["final_url"] = decoded
                try: result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception: pass
                return result

            if looks_final(current_url) and current_url != url:
                result["final_url"] = current_url
                try: result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception: pass
                return result

            try: content = await page.content()
            except Exception: content = ""

            if any(k in content.lower() for k in ("captcha","recaptcha","hcaptcha","i am not a robot")):
                result["captcha_detected"] = True
                try: result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception: pass
                return result

            await page.wait_for_timeout(1000)
        try: result["screenshot_b64"] = await take_screenshot_b64(page)
        except Exception: pass
        return result
    except Exception:
        logger.exception("Error in bypass_once")
        return result

# === Endpoint ===
@app.post("/bypass", response_model=BypassResponse)
async def bypass_endpoint(req: BypassRequest, x_api_key: Optional[str] = Header(None)):
    if API_KEY and (not x_api_key or x_api_key != API_KEY):
        raise HTTPException(status_code=401, detail="Missing/invalid API key")
    url = str(req.url)
    attempts = max(1,min(10,req.attempts or DEFAULT_ATTEMPTS))
    headless = bool(req.headless)
    include_screenshot = bool(req.include_screenshot)
    logger.info("Bypass request url=%s attempts=%s headless=%s", url, attempts, headless)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox","--disable-dev-shm-usage"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        final = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None}
        attempt_made = 0
        try:
            for i in range(1, attempts+1):
                attempt_made = i
                try: await page.mouse.move(100,100)
                except Exception: pass
                res = await bypass_once(page,url,i,progress_logger=lambda t: logger.info("BYPASS: %s", t))
                final.update(res)
                if res.get("captcha_detected") or looks_final(res.get("final_url")): break
                if i<attempts:
                    backoff = (2**i)+0.5*i
                    logger.info("Waiting %.1fs before retry %d", backoff,i+1)
                    await page.wait_for_timeout(int(backoff*1000))
                    try: await page.reload(timeout=5000)
                    except Exception: pass
        finally:
            try: await context.close()
            except Exception: pass
            try: await browser.close()
            except Exception: pass

        b64 = final.get("screenshot_b64") if include_screenshot else None
        return BypassResponse(
            final_url = final.get("final_url") or url,
            raw_last_url = final.get("raw_last_url") or url,
            captcha_detected = bool(final.get("captcha_detected")),
            screenshot_b64 = b64,
            attempts_made = attempt_made
        )

@app.get("/health")
async def health():
    return {"status":"ok"}

# === Web UI ===
@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>GPLinks Bypass</title>
        <style>
            body{font-family:sans-serif; padding:20px;}
            input[type=text]{width:60%; padding:8px;}
            button{padding:8px 12px;}
            #spinner{display:none;}
        </style>
    </head>
    <body>
        <h1>GPLinks Bypass Service</h1>
        <input type="text" id="gplink" placeholder="Enter GPLinks URL"/>
        <label><input type="checkbox" id="screenshot"/> Include Screenshot</label>
        <button onclick="startBypass()">Start</button>
        <div id="spinner">⏳ Bypassing...</div>
        <pre id="result"></pre>

        <script>
        async function startBypass(){
            const url=document.getElementById('gplink').value;
            const include_screenshot=document.getElementById('screenshot').checked;
            if(!url){ alert("Enter a GPLinks URL"); return; }
            document.getElementById('spinner').style.display='inline';
            document.getElementById('result').textContent="";
            try{
                const res=await fetch("/bypass",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url,include_screenshot})});
                const data=await res.json();
                let out=`✅ Final URL: ${data.final_url}\\nAttempts Made: ${data.attempts_made}\\nCaptcha Detected: ${data.captcha_detected}`;
                if(data.screenshot_b64){
                    out+="\\nScreenshot: ";
                    out+=`<a href="data:image/png;base64,${data.screenshot_b64}" download="screenshot.png">Download</a>`;
                }
                document.getElementById('result').innerHTML=out;
            }catch(e){
                document.getElementById('result').textContent="⚠️ Error: "+e;
            }finally{
                document.getElementById('spinner').style.display='none';
            }
        }
        </script>
    </body>
    </html>
    """