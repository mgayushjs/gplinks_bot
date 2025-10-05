# web_bypass.py
"""
Complete FastAPI + Playwright service and UI:
 - GET  /        -> simple web UI (sends POST to /bypass)
 - POST /bypass  -> bypass logic (click "Get link" etc.) returns JSON
 - GET  /health  -> {"status":"ok"}

Set API_KEY env var to require x-api-key header (optional).
"""
import os
import re
import time
import base64
import logging
from typing import Optional, List, Set
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, AnyHttpUrl
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError, Response

# -----------------------
# Config
# -----------------------
DEBUG_LOGGING = False   # set True only for local debugging
LOG_LEVEL = logging.DEBUG if DEBUG_LOGGING else logging.INFO
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("web-bypass")

APP_PORT = int(os.environ.get("PORT", 8080))

DEFAULT_HEADLESS = True
NAV_TIMEOUT = 60_000        # ms
CLICK_TIMEOUT = 12_000      # ms
WAIT_AFTER_OPEN = 5         # seconds wait after opening initial gplinks page
MAX_TOTAL_WAIT = 90         # seconds per attempt
DEFAULT_ATTEMPTS = 3
MAX_NAV_HISTORY = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"

API_KEY = os.environ.get("API_KEY")

# -----------------------
# FastAPI app
# -----------------------
app = FastAPI(title="GPLinks Bypass Full (UI + API)")

# -----------------------
# Models
# -----------------------
class BypassRequest(BaseModel):
    url: AnyHttpUrl
    attempts: Optional[int] = DEFAULT_ATTEMPTS
    headless: Optional[bool] = DEFAULT_HEADLESS
    include_screenshot: Optional[bool] = False

class BypassResponse(BaseModel):
    final_url: str
    raw_last_url: str
    captcha_detected: bool
    screenshot_b64: Optional[str] = None
    attempts_made: int
    nav_history: Optional[List[str]] = None

# -----------------------
# Helpers
# -----------------------
def safe_log(msg: str):
    if DEBUG_LOGGING:
        logger.debug(msg)

def looks_final(u: Optional[str]) -> bool:
    if not u:
        return False
    s = u.lower()
    return ("gplinks.co" not in s) and ("get2.in" not in s) and ("gplinks" not in s)

def resolve_href(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except Exception:
        return href

async def take_screenshot_b64(page: Page) -> str:
    content = await page.screenshot(full_page=True)
    return base64.b64encode(content).decode()

# click helper: find elements with "get link" style text and click them
async def try_click_getlink_elements(page: Page):
    patterns = ["get link", "get-link", "getlink", "get now", "show link", "click here",
                "continue", "open link", "get url", "get code", "generate", "download"]
    try:
        els = await page.query_selector_all("a, button, input[type=button], input[type=submit]")
    except Exception:
        els = []

    base_url = page.url
    for el in els:
        try:
            txt = ""
            aria = ""
            href = None
            try:
                txt = (await el.inner_text() or "").strip().lower()
            except Exception:
                txt = ""
            try:
                aria = (await el.get_attribute("aria-label") or "").strip().lower()
            except Exception:
                aria = ""
            try:
                href = await el.get_attribute("href")
            except Exception:
                href = None

            combined = f"{txt} {aria}".strip()
            if any(p in combined for p in patterns):
                safe_log(f"click candidate: text='{combined[:80]}' href={href}")
                # try clicking
                try:
                    await el.click(timeout=CLICK_TIMEOUT)
                except Exception:
                    try:
                        await page.evaluate("(e)=>e.click()", el)
                    except Exception:
                        pass
                # wait briefly for navigation/xhr
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    await page.wait_for_timeout(1200)

                # if navigated
                new_url = page.url
                if new_url and new_url != base_url:
                    # try to prefer heroku generate if present
                    try:
                        txtall = await page.content()
                        m = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', txtall)
                        if m:
                            return m.group(0)
                    except Exception:
                        pass
                    return new_url

                # if no navigation, check href
                if href:
                    full = resolve_href(base_url, href)
                    if looks_final(full) or "herokuapp.com" in full or "/generate?code=" in full:
                        return full

                # scan page content for heroku link
                try:
                    txtall = await page.content()
                    m2 = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', txtall)
                    if m2:
                        return m2.group(0)
                except Exception:
                    pass
        except Exception:
            pass
    return None

# network listener helper
def make_response_listener(found_set: Set[str]):
    async def on_response(resp: Response):
        try:
            u = resp.url
            if "herokuapp.com" in u or "/generate?code=" in u:
                found_set.add(u)
            # best-effort body scan for small text/json responses
            try:
                ct = resp.headers.get("content-type", "")
                if ("json" in ct or "text" in ct) and len(u) < 800:
                    txt = await resp.text()
                    m = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', txt)
                    if m:
                        found_set.add(m.group(0))
            except Exception:
                pass
        except Exception:
            pass
    return on_response

# main bypass attempt that follows links and returns last opened URL
async def bypass_once(page: Page, url: str, attempt_num: int):
    result = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None}
    nav_history: List[str] = []
    found_network_urls: Set[str] = set()

    listener = make_response_listener(found_network_urls)
    page.on("response", listener)

    try:
        logger.info(f"Bypass attempt #{attempt_num} start")
        try:
            await page.goto(url, timeout=NAV_TIMEOUT)
        except PlaywrightTimeoutError:
            safe_log("page.goto timeout")
        except Exception as e:
            safe_log(f"page.goto exception: {e}")

        # wait exactly WAIT_AFTER_OPEN seconds (you requested 5s)
        try:
            await page.wait_for_timeout(WAIT_AFTER_OPEN * 1000)
        except Exception:
            pass

        nav_history.append(page.url)
        start_time = time.time()
        last_url = page.url

        def push_history(u: str):
            if not u:
                return
            if not nav_history or nav_history[-1] != u:
                nav_history.append(u)

        while time.time() - start_time < MAX_TOTAL_WAIT and len(nav_history) < MAX_NAV_HISTORY:
            current_url = page.url
            result["raw_last_url"] = current_url

            # if network discovered interesting url, return it
            if found_network_urls:
                chosen = sorted(found_network_urls)[0]
                result["final_url"] = chosen
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                result["nav_history"] = nav_history
                return result

            # try clicking get link elements
            click_res = await try_click_getlink_elements(page)
            if click_res:
                push_history(page.url)
                result["final_url"] = click_res
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                result["nav_history"] = nav_history
                return result

            # update history if changed
            if page.url != last_url:
                last_url = page.url
                push_history(last_url)

            # scan page for heroku generate link
            try:
                cont = await page.content()
                m = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', cont)
                if m:
                    result["final_url"] = m.group(0)
                    try:
                        result["screenshot_b64"] = await take_screenshot_b64(page)
                    except Exception:
                        pass
                    result["nav_history"] = nav_history
                    return result
            except Exception:
                pass

            # detect captcha-like content
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
                result["nav_history"] = nav_history
                return result

            # if left shortener domain, give a short window for dynamic link creation
            if looks_final(current_url) and current_url != url:
                extra_end = time.time() + 8
                while time.time() < extra_end:
                    if found_network_urls:
                        chosen = sorted(found_network_urls)[0]
                        result["final_url"] = chosen
                        try:
                            result["screenshot_b64"] = await take_screenshot_b64(page)
                        except Exception:
                            pass
                        result["nav_history"] = nav_history
                        return result
                    click_res = await try_click_getlink_elements(page)
                    if click_res:
                        push_history(page.url)
                        result["final_url"] = click_res
                        try:
                            result["screenshot_b64"] = await take_screenshot_b64(page)
                        except Exception:
                            pass
                        result["nav_history"] = nav_history
                        return result
                    await page.wait_for_timeout(1000)

                result["final_url"] = page.url
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                result["nav_history"] = nav_history
                return result

            # fallback: try some generic clicks and continue
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
                push_history(last_url)

            await page.wait_for_timeout(800)

        # ended loop: return last known URL
        result["final_url"] = page.url or result["final_url"]
        try:
            result["screenshot_b64"] = await take_screenshot_b64(page)
        except Exception:
            pass
        result["nav_history"] = nav_history
        return result

    except Exception as e:
        logger.exception("Error in bypass_once")
        raise e
    finally:
        try:
            page.off("response", listener)
        except Exception:
            pass

# -----------------------
# POST /bypass (with robust error handling)
# -----------------------
@app.post("/bypass", response_model=BypassResponse)
async def bypass_endpoint(req: BypassRequest, x_api_key: Optional[str] = Header(None), request: Request = None):
    try:
        # API key check
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

        logger.info("Received bypass request")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()

            final = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None}
            attempt_made = 0
            try:
                for i in range(1, attempts + 1):
                    attempt_made = i
                    # small human-like action
                    try:
                        await page.mouse.move(120, 120)
                    except Exception:
                        pass

                    res = await bypass_once(page, url, i)
                    final.update(res)

                    if res.get("captcha_detected"):
                        break
                    if looks_final(final.get("final_url")) and "gplinks" not in (final.get("final_url") or "").lower():
                        break

                    if i < attempts:
                        backoff = (2 ** i) + 0.5 * i
                        logger.info("Waiting %.1fs before retry %d", backoff, i + 1)
                        await page.wait_for_timeout(int(backoff * 1000))
                        try:
                            await page.reload(timeout=5000)
                            await page.wait_for_timeout(1000)
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

        b64 = final.get("screenshot_b64") if include_screenshot else None

        return BypassResponse(
            final_url=final.get("final_url") or url,
            raw_last_url=final.get("raw_last_url") or url,
            captcha_detected=bool(final.get("captcha_detected")),
            screenshot_b64=b64,
            attempts_made=attempt_made,
            nav_history=final.get("nav_history") or []
        )

    except HTTPException as he:
        # bubble up HTTPExceptions with their detail
        raise he
    except Exception as e:
        logger.exception("Unhandled error in /bypass")
        # Return a JSON response with error detail so the UI receives it
        return JSONResponse(status_code=500, content={"detail": str(e)})

# -----------------------
# Health and Web UI
# -----------------------
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def index():
    # Simple UI: sends JSON POST to /bypass and displays response or error
    return """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>GPLinks Bypass — UI</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:0;padding:20px;background:#f5f7fb}
.card{max-width:900px;margin:20px auto;padding:20px;background:#fff;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,0.06)}
input[type=text]{width:66%;padding:10px;border-radius:6px;border:1px solid #ddd}
button{padding:9px 12px;margin-left:8px;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer}
label{margin-left:8px}
#spinner{display:none;margin-top:10px}
pre{background:#f7f8fb;padding:12px;border-radius:6px;white-space:pre-wrap}
img.debug{max-width:100%;margin-top:8px;border-radius:6px}
</style>
</head>
<body>
<div class="card">
  <h2>GPLinks Bypass</h2>
  <p>Paste a GPLinks URL and click <strong>Start</strong>. Use "Include screenshot" to save debug images.</p>

  <div>
    <input id="gplink" type="text" placeholder="https://gplinks.co/..." />
    <button onclick="startBypass()">Start</button>
  </div>

  <div style="margin-top:8px">
    <label>Attempts: <input id="attempts" type="number" value="3" min="1" max="10" style="width:72px" /></label>
    <label style="margin-left:12px"><input id="headless" type="checkbox" checked/> Headless</label>
    <label style="margin-left:12px"><input id="screenshot" type="checkbox" /> Include screenshot</label>
    <label style="margin-left:12px">API Key: <input id="apikey" type="text" placeholder="(optional)"/></label>
  </div>

  <div id="spinner">⏳ Bypassing — please wait...</div>
  <div id="output" style="margin-top:12px"></div>
</div>

<script>
function esc(s){ return (s||'').replace(/'/g,"\\'").replace(/"/g,'\\"'); }

async function startBypass(){
  const url = document.getElementById('gplink').value.trim();
  if(!url){ alert('Enter a GPLinks URL'); return; }
  document.getElementById('output').innerHTML = '';
  document.getElementById('spinner').style.display = 'block';

  const attempts = parseInt(document.getElementById('attempts').value || '3', 10);
  const headless = document.getElementById('headless').checked;
  const include_screenshot = document.getElementById('screenshot').checked;
  const apikey = document.getElementById('apikey').value.trim();

  try {
    const headers = {'Content-Type':'application/json'};
    if(apikey) headers['x-api-key'] = apikey;

    const res = await fetch('/bypass', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({ url: url, attempts: attempts, headless: headless, include_screenshot: include_screenshot })
    });

    if(!res.ok){
      const err = await res.json().catch(()=>({detail:res.statusText}));
      document.getElementById('output').innerText = 'Error: ' + (err.detail || JSON.stringify(err));
      return;
    }

    const data = await res.json();
    let html = `<pre>✅ Final URL: ${data.final_url}\nAttempts: ${data.attempts_made}\nCaptcha Detected: ${data.captcha_detected}\nRaw Last URL: ${data.raw_last_url}\nNavigation history: ${JSON.stringify(data.nav_history||[])} </pre>`;
    html += `<p><button onclick="window.open('${esc(data.final_url)}','_blank')">Open final URL</button></p>`;
    if(data.screenshot_b64){
      html += `<p><a href="data:image/png;base64,${data.screenshot_b64}" download="screenshot.png">Download screenshot</a></p>`;
      html += `<p><img class="debug" src="data:image/png;base64,${data.screenshot_b64}" /></p>`;
    }
    document.getElementById('output').innerHTML = html;
  } catch (e) {
    document.getElementById('output').innerText = 'Error: ' + e;
  } finally {
    document.getElementById('spinner').style.display = 'none';
  }
}
</script>
</body>
</html>
"""