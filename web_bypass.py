# web_bypass.py
"""
FastAPI web service with Heroku-target extraction for GPLinks/get2.in chains.
Endpoints:
 - GET /            -> small HTML UI (enter GPLinks URL)
 - POST /bypass     -> JSON API to bypass a link (returns final_url, screenshot etc.)
 - GET /health      -> {"status":"ok"}

Notes:
 - Optional API key: set env var API_KEY; clients must send header x-api-key.
 - Ensure playwright browsers are installed (playwright install --with-deps chromium).
"""

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

import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web-bypass")

app = FastAPI(title="GPLinks Bypass Service")

# === Config (tweak if needed) ===
DEFAULT_HEADLESS = True
NAV_TIMEOUT = 60_000         # ms
CLICK_TIMEOUT = 12_000       # ms
MAX_TOTAL_WAIT = 60         # seconds per attempt
DEFAULT_ATTEMPTS = 3
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"

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
    # consider final when it's no longer a "gplinks" short-domain
    return ("gplinks.co" not in u_l) and ("gplinks" not in u_l)

def try_decode_get2_in(url: str) -> Optional[str]:
    """
    Try to decode get2.in style targets (base64 or URL encoded).
    """
    try:
        parsed = urlparse(url)
        if "get2.in" not in parsed.netloc.lower():
            return None
        query = parsed.query or parsed.path.split("?", 1)[-1]
        if not query:
            return None
        candidate = None
        if "=" in query:
            parts = query.split("&")
            # prefer long value parts
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    if len(v) > 8:
                        candidate = v
                        break
            if not candidate:
                candidate = parts[-1].split("=", 1)[-1]
        else:
            candidate = query
        if not candidate:
            return None
        cand = unquote(candidate)
        # try base64 urlsafe decode
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
        # fallback: if cand itself looks like URL
        if cand.startswith("http"):
            return cand
    except Exception:
        logger.exception("decode get2.in failed")
    return None

async def take_screenshot_b64(page: Page) -> str:
    b = await page.screenshot(full_page=True)
    return base64.b64encode(b).decode()

# === New helper: search page for heroku generate link ===
async def find_heroku_generate_in_page(page: Page) -> Optional[str]:
    """
    Inspect the current page for any herokuapp URL (preferably one containing '/generate?code=').
    Returns the discovered URL or None.
    """
    try:
        # get page HTML/content
        content = ""
        try:
            content = await page.content()
        except Exception:
            content = ""

        # 1) look for direct generate?code= links in HTML
        m = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', content, re.IGNORECASE)
        if m:
            return m.group(0)

        # 2) any herokuapp link (candidate)
        m2 = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com[^\s"\'<>"]*', content, re.IGNORECASE)
        if m2:
            candidate = m2.group(0)
            # try to find a nearby code/token in the page to append if needed
            code_match = re.search(r'(?:code|token)\s*[:=]\s*[\'"]([A-Za-z0-9_\-]{8,256})[\'"]', content)
            if code_match:
                return candidate.rstrip('/') + '/generate?code=' + code_match.group(1)
            return candidate

        # 3) detect base64-like strings that decode to heroku generate url
        b64_matches = re.findall(r'["\']([A-Za-z0-9_\-]{16,}={0,2})["\']', content)
        for cand in b64_matches:
            try:
                padding = (-len(cand)) % 4
                bs = cand.encode()
                if padding:
                    bs += b"=" * padding
                dec = base64.urlsafe_b64decode(bs).decode(errors="ignore")
                if "herokuapp.com" in dec and "generate" in dec:
                    return dec
            except Exception:
                pass

        # 4) evaluate page innerText (helpful to catch JS-constructed strings)
        try:
            js_text = await page.evaluate("() => { return document.body.innerText || document.documentElement.innerText || '' }")
            if js_text:
                m3 = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s]+generate\?code=[A-Za-z0-9_\-]+', js_text, re.IGNORECASE)
                if m3:
                    return m3.group(0)
        except Exception:
            pass

    except Exception:
        logger.exception("Error while searching for heroku generate URL on page")
    return None

# === Core attempt function (integrates heroku search) ===
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

        # try early redirect button click (common on gplinks)
        try:
            btn = await page.wait_for_selector("a#btn-main, button#btn-main, a[role='button']", timeout=15000)
            if btn:
                try:
                    if progress_logger:
                        progress_logger(f"[attempt {attempt_num}] found redirect button; clicking")
                    await btn.click(timeout=CLICK_TIMEOUT)
                    await page.wait_for_timeout(1500)
                except Exception:
                    logger.debug("redirect button click failed")
        except Exception:
            pass

        start = time.time()
        last_url = page.url
        while time.time() - start < MAX_TOTAL_WAIT:
            current_url = page.url
            result["raw_last_url"] = current_url

            # 1) handle get2.in decoding if encountered
            decoded = try_decode_get2_in(current_url)
            if decoded:
                # navigate to decoded target so we can inspect it
                try:
                    if progress_logger:
                        progress_logger(f"[attempt {attempt_num}] decoded get2.in -> navigating to decoded target")
                    await page.goto(decoded, timeout=15000)
                except Exception:
                    pass

                # try to find heroku link on decoded page
                heroku_target = await find_heroku_generate_in_page(page)
                if heroku_target:
                    result["final_url"] = heroku_target
                else:
                    result["final_url"] = decoded
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                return result

            # 2) if we left gplinks domain, consider it final — but first attempt to find heroku link
            if looks_final(current_url) and current_url != url:
                # inspect current page for heroku target
                heroku_target = None
                try:
                    heroku_target = await find_heroku_generate_in_page(page)
                except Exception:
                    heroku_target = None

                if heroku_target:
                    result["final_url"] = heroku_target
                else:
                    result["final_url"] = current_url

                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                return result

            # 3) detect captcha in page content
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

            # 4) try clicking common selectors (anchors/buttons)
            selectors = [
                "a#btn-main", "a[href*='redirect']", "a[href*='http']",
                "a.btn", "button#btn-main", "button", "input[type=submit]", "a[role='button']"
            ]
            clicked = False
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        try:
                            await el.click(timeout=CLICK_TIMEOUT)
                            clicked = True
                            await page.wait_for_timeout(1200)
                        except Exception:
                            pass
                except Exception:
                    pass

            # 5) network idle wait
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass

            # 6) sniff meta refresh
            try:
                m = re.search(r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]*content=["\']?([^"\']+)["\']?', content, re.IGNORECASE)
                if m:
                    mm = re.search(r'url=([^;\'"]+)', m.group(1), re.IGNORECASE)
                    if mm:
                        target = urljoin(page.url, mm.group(1).strip())
                        try:
                            await page.goto(target, timeout=15000)
                            continue
                        except Exception:
                            pass
            except Exception:
                pass

            # 7) sniff JS redirect in source
            try:
                js_match = re.search(r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', content, re.IGNORECASE)
                if js_match:
                    target = urljoin(page.url, js_match.group(1).strip())
                    try:
                        await page.goto(target, timeout=15000)
                        continue
                    except Exception:
                        pass
            except Exception:
                pass

            # 8) find external anchors and try navigate — if we navigate, check heroku there
            try:
                anchors = await page.query_selector_all("a")
                for a in anchors:
                    try:
                        href = await a.get_attribute("href")
                        if not href:
                            continue
                        if href.startswith("javascript:") or href.startswith("#"):
                            continue
                        full = urljoin(page.url, href)
                        if looks_final(full):
                            try:
                                await page.goto(full, timeout=15000)
                                # after navigating to potential external anchor, try heroku discovery
                                heroku_candidate = await find_heroku_generate_in_page(page)
                                if heroku_candidate:
                                    result["final_url"] = heroku_candidate
                                    try:
                                        result["screenshot_b64"] = await take_screenshot_b64(page)
                                    except Exception:
                                        pass
                                    return result
                                # if landing page appears final, return it
                                if looks_final(page.url):
                                    result["final_url"] = page.url
                                    try:
                                        result["screenshot_b64"] = await take_screenshot_b64(page)
                                    except Exception:
                                        pass
                                    return result
                            except Exception:
                                # best-effort: return discovered anchor href
                                result["final_url"] = full
                                return result
                    except Exception:
                        pass
            except Exception:
                pass

            if page.url != last_url:
                last_url = page.url

            await page.wait_for_timeout(1000)

        # timeout reached: best-effort return last page and a screenshot
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
    # API key check (optional)
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
                # tiny human-like action
                try:
                    await page.mouse.move(100, 100)
                except Exception:
                    pass

                res = await bypass_once(page, url, i, progress_logger=lambda t: logger.info("BYPASS: %s", t))
                final.update(res)
                if res.get("captcha_detected") or looks_final(res.get("final_url")):
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

        # prepare response
        b64 = final.get("screenshot_b64") if include_screenshot else None

        return BypassResponse(
            final_url=final.get("final_url") or url,
            raw_last_url=final.get("raw_last_url") or url,
            captcha_detected=bool(final.get("captcha_detected")),
            screenshot_b64=b64,
            attempts_made=attempt_made,
        )

@app.get("/health")
async def health():
    return {"status": "ok"}

# === Web UI ===
@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>GPLinks Bypass</title>
        <meta charset="utf-8" />
        <style>
            body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; padding:20px; max-width:900px;margin:auto;}
            input[type=text]{width:70%; padding:8px; margin-right:8px;}
            button{padding:8px 12px;}
            #spinner{display:none;margin-top:10px;}
            pre{background:#f7f7f8;padding:12px;border-radius:6px; white-space:pre-wrap;}
            .small{font-size:0.9rem;color:#666;}
        </style>
    </head>
    <body>
        <h1>GPLinks Bypass Service</h1>
        <p class="small">Paste a GPLinks URL and click <strong>Start</strong>. Optional: include a debug screenshot.</p>
        <input type="text" id="gplink" placeholder="https://gplinks.co/..." />
        <label><input type="checkbox" id="screenshot" /> Include Screenshot</label>
        <button onclick="startBypass()">Start</button>
        <div id="spinner">⏳ Bypassing — this may take a few seconds...</div>
        <div id="output" style="margin-top:12px;"></div>

        <script>
        async function startBypass(){
            const url = document.getElementById('gplink').value.trim();
            const include_screenshot = document.getElementById('screenshot').checked;
            if(!url){ alert("Please enter a GPLinks URL."); return; }
            document.getElementById('spinner').style.display = 'block';
            document.getElementById('output').innerHTML = '';
            try{
                const res = await fetch("/bypass", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ url: url, include_screenshot: include_screenshot, attempts: 3 })
                });
                if(!res.ok){
                    const err = await res.json().catch(()=>({detail:res.statusText}));
                    document.getElementById('output').innerText = "Error: " + (err.detail || JSON.stringify(err));
                    return;
                }
                const data = await res.json();
                let html = `<pre>✅ Final URL: ${data.final_url}\nAttempts Made: ${data.attempts_made}\nCaptcha Detected: ${data.captcha_detected}\nRaw Last URL: ${data.raw_last_url}</pre>`;
                if(data.screenshot_b64){
                    html += `<p><a href="data:image/png;base64,${data.screenshot_b64}" download="screenshot.png">Download debug screenshot</a></p>`;
                    html += `<p><img src="data:image/png;base64,${data.screenshot_b64}" style="max-width:100%; border:1px solid #ddd;"/></p>`;
 