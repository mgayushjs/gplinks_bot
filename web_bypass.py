# web_bypass.py
"""
GPLinks bypass service (single-file, no templates).
UI: GET  /          -> HTML page (JS posts JSON to /bypass)
API: POST /bypass   -> { url, attempts, headless, include_screenshot }
Health: GET /health -> {"status":"ok"}

Flow implemented:
  1) open GPLinks URL
  2) for 3 rounds: wait 15s, try clicking verification buttons/anchors
  3) wait for GPLinks to load again
  4) click "Get Link" (or similar)
  5) wait for final navigation / networkidle and return the final URL (not intermediate)
"""

import os
import re
import time
import base64
import logging
from typing import Optional, List
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, AnyHttpUrl
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gplinks-bypass")

app = FastAPI(title="GPLinks Bypass (embedded UI)")

# --- Config ---
WAIT_STEP_SECONDS = 15        # user said 15s waits between verification steps
VERIFY_ROUNDS = 3             # number of verify clicks to perform
CLICK_TIMEOUT = 12_000        # ms
NAV_TIMEOUT = 60_000          # ms
MAX_TOTAL_WAIT = 120          # seconds per attempt to reach final
DEFAULT_ATTEMPTS = 2
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
API_KEY = os.environ.get("API_KEY")  # optional

# --- Pydantic models ---
class BypassRequest(BaseModel):
    url: AnyHttpUrl
    attempts: Optional[int] = DEFAULT_ATTEMPTS
    headless: Optional[bool] = True
    include_screenshot: Optional[bool] = False

class BypassResponse(BaseModel):
    final_url: str
    raw_last_url: str
    captcha_detected: bool
    screenshot_b64: Optional[str] = None
    attempts_made: int
    nav_history: Optional[List[str]] = None

# --- Helpers ---
def looks_final(u: Optional[str]) -> bool:
    if not u:
        return False
    u_l = u.lower()
    # treat final as not being gplinks nor other well-known short intermediate
    return ("gplinks.co" not in u_l) and ("gplinks" not in u_l) and ("procinehub.com" not in u_l)

def resolve_href(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except Exception:
        return href

async def take_screenshot_b64(page: Page) -> str:
    b = await page.screenshot(full_page=True)
    return base64.b64encode(b).decode()

# clickable selectors that commonly appear on these pages
VERIFY_SELECTORS = [
    "a#linkbtn", "a#btn-main", "a.btn", "button.btn", "button", "a[role='button']",
    "a[href*='redirect']", "a[href*='get']", "a:has-text('Verify')", "button:has-text('Verify')",
    "a:has-text('Click here')", "a:has-text('Continue')", "button:has-text('Continue')",
    "a:has-text('Get Link')", "button:has-text('Get Link')"
]

GETLINK_SELECTORS = [
    "a:has-text('Get Link')", "a:has-text('Get-Link')", "a:has-text('Get link')",
    "a#btn-main", "a#linkbtn", "a[href*='generate']", "a[href*='final']",
    "button:has-text('Get Link')", "button:has-text('Open Link')"
]

# --- Core attempt function implementing exact flow requested ---
async def one_attempt_bypass(page: Page, url: str, progress_logger=None):
    """
    Open the GPLinks URL, follow the multi-step flow:
      - wait WAIT_STEP_SECONDS
      - try VERIFY_ROUNDS of verification clicks
      - detect return to gplinks, then click Get Link
      - wait for the final destination (non-gplinks) and return it
    Returns dict with final_url, raw_last_url, captcha_detected, screenshot_b64, nav_history
    """
    result = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None, "nav_history": []}
    nav_history = result["nav_history"]

    try:
        if progress_logger:
            progress_logger(f"goto {url}")
        try:
            await page.goto(url, timeout=NAV_TIMEOUT)
        except PlaywrightTimeoutError:
            logger.warning("initial goto timeout")
        except Exception:
            logger.exception("initial goto exception")

        nav_history.append(page.url)

        # execute VERIFY_ROUNDS: for each round wait WAIT_STEP_SECONDS then try clicking verification elements
        for round_i in range(1, VERIFY_ROUNDS + 1):
            if progress_logger:
                progress_logger(f"round {round_i}: waiting {WAIT_STEP_SECONDS}s before verify-clicks")
            # wait the required time
            await page.wait_for_timeout(WAIT_STEP_SECONDS * 1000)

            # attempt multiple selector clicks in order to trigger progression
            clicked_any = False
            for sel in VERIFY_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        try:
                            if progress_logger:
                                progress_logger(f"round {round_i}: clicking selector {sel}")
                            await el.click(timeout=CLICK_TIMEOUT)
                            clicked_any = True
                            # small wait for network / DOM changes
                            await page.wait_for_timeout(1500)
                        except Exception:
                            # fallback evaluate click by JS if native click fails
                            try:
                                await page.evaluate("(el)=>el.click()", el)
                                clicked_any = True
                                await page.wait_for_timeout(1500)
                            except Exception:
                                pass
                except Exception:
                    pass
            # append current URL to nav_history
            nav_history.append(page.url)

        # after verify rounds we expect the flow to return us to a GPLinks landing where the "Get Link" button appears.
        # give the page a little time to stabilize
        if progress_logger:
            progress_logger("waiting 3s to stabilize after verify rounds")
        await page.wait_for_timeout(3000)

        # try detecting final Get Link; if intermediate pages appear (like procinehub) we try to navigate forward
        total_wait_start = time.time()
        while time.time() - total_wait_start < MAX_TOTAL_WAIT:
            # first, try to find and click the "Get Link" style selectors
            for sel in GETLINK_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        # resolve href (if present) first
                        href = None
                        try:
                            href = await el.get_attribute("href")
                        except Exception:
                            href = None
                        if progress_logger:
                            progress_logger(f"Found getlink candidate selector {sel} (href={href}) - clicking")
                        try:
                            await el.click(timeout=CLICK_TIMEOUT)
                        except Exception:
                            try:
                                await page.evaluate("(el)=>el.click()", el)
                            except Exception:
                                pass
                        # wait for navigation or XHRs
                        try:
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            await page.wait_for_timeout(1500)

                        nav_history.append(page.url)
                        # if page.url appears final (not gplinks/procinehub), return it
                        current_url = page.url
                        if looks_final(current_url):
                            result["final_url"] = current_url
                            result["raw_last_url"] = current_url
                            try:
                                result["screenshot_b64"] = await take_screenshot_b64(page)
                            except Exception:
                                pass
                            return result
                        # if href looked like final, resolve & return
                        if href:
                            full = resolve_href(page.url, href)
                            if looks_final(full):
                                result["final_url"] = full
                                result["raw_last_url"] = page.url
                                try:
                                    result["screenshot_b64"] = await take_screenshot_b64(page)
                                except Exception:
                                    pass
                                return result

                except Exception:
                    pass

            # Some pages show the final link inside page content or via XHR. Try to detect a final URL in HTML/text.
            try:
                content = await page.content()
                # look for typical final link patterns (heroku generate, drive, googleusercontent, etc.)
                m = re.search(r'https?://[^\s"\'<>]+/(?:generate\?code=|drive|googleusercontent|dropboxusercontent|redirect)[^\s"\'<>]*', content, re.IGNORECASE)
                if m:
                    candidate = m.group(0)
                    # prefer candidate if not intermediate
                    if looks_final(candidate):
                        result["final_url"] = candidate
                        result["raw_last_url"] = page.url
                        try:
                            result["screenshot_b64"] = await take_screenshot_b64(page)
                        except Exception:
                            pass
                        return result
            except Exception:
                pass

            # If current page is an intermediate like procinehub, attempt to click any link on that page that leads outward
            cur = page.url or ""
            if "procinehub" in cur.lower() or "intermediate" in cur.lower():
                # try all anchors on page, follow first that looks final
                try:
                    anchors = await page.query_selector_all("a")
                    for a in anchors:
                        try:
                            href = await a.get_attribute("href")
                            if not href:
                                continue
                            full = resolve_href(page.url, href)
                            if looks_final(full):
                                if progress_logger:
                                    progress_logger(f"Following anchor href {full} from intermediate page")
                                try:
                                    await page.goto(full, timeout=15000)
                                except Exception:
                                    # try click
                                    try:
                                        await a.click(timeout=CLICK_TIMEOUT)
                                    except Exception:
                                        pass
                                nav_history.append(page.url)
                                if looks_final(page.url):
                                    result["final_url"] = page.url
                                    try:
                                        result["screenshot_b64"] = await take_screenshot_b64(page)
                                    except Exception:
                                        pass
                                    return result
                        except Exception:
                            pass
                except Exception:
                    pass

            # if nothing found yet wait a bit more for JS/XHR to complete, then re-loop
            await page.wait_for_timeout(1000)

        # timed out trying to find final link - return best-effort (but avoid procinehub if possible)
        last = page.url or url
        if "procinehub" in (last or "").lower():
            # try to inspect anchors and return first non-procinehub external link
            try:
                anchors = await page.query_selector_all("a")
                for a in anchors:
                    try:
                        href = await a.get_attribute("href")
                        if not href:
                            continue
                        full = resolve_href(page.url, href)
                        if "procinehub" not in full.lower() and "gplinks" not in full.lower():
                            result["final_url"] = full
                            result["raw_last_url"] = page.url
                            try:
                                result["screenshot_b64"] = await take_screenshot_b64(page)
                            except Exception:
                                pass
                            return result
                    except Exception:
                        pass
            except Exception:
                pass

        # fallback: return whatever page.url currently is (but flagged)
        result["final_url"] = last
        result["raw_last_url"] = last
        try:
            result["screenshot_b64"] = await take_screenshot_b64(page)
        except Exception:
            pass
        return result

    except Exception as e:
        logger.exception("one_attempt_bypass error")
        result["final_url"] = url
        result["raw_last_url"] = page.url if page else url
        try:
            result["screenshot_b64"] = await take_screenshot_b64(page)
        except Exception:
            pass
        return result

# --- Endpoint: POST /bypass ---
@app.post("/bypass")
async def bypass_endpoint(req: BypassRequest, x_api_key: Optional[str] = Header(None)):
    # API key check (optional)
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Missing/invalid API key")

    url = str(req.url)
    attempts = max(1, min(6, int(req.attempts or DEFAULT_ATTEMPTS)))
    headless = bool(req.headless)
    include_screenshot = bool(req.include_screenshot)

    logger.info("Bypass request: url=%s attempts=%s headless=%s", url, attempts, headless)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        final = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None, "nav_history": []}
        attempt_made = 0
        try:
            for i in range(1, attempts + 1):
                attempt_made = i
                # small human-like move
                try:
                    await page.mouse.move(100, 100)
                except Exception:
                    pass

                res = await one_attempt_bypass(page, url, progress_logger=lambda t: logger.info("BYPASS: %s", t))
                # merge results
                final.update(res)
                final["nav_history"] = res.get("nav_history", [])

                # if result looks final (not gplinks/procinehub) - done
                if looks_final(final.get("final_url")):
                    break

                # otherwise prepare for next attempt: reload and small backoff
                if i < attempts:
                    backoff = (2 ** i) + 0.5 * i
                    logger.info("Retrying in %.1fs", backoff)
                    await page.wait_for_timeout(int(backoff * 1000))
                    try:
                        await page.reload(timeout=5000)
                        await page.wait_for_timeout(1000)
                    except Exception:
                        pass

        finally:
            # optionally get screenshot
            if include_screenshot and page:
                try:
                    final["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    final["screenshot_b64"] = None
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

    # build response
    resp = BypassResponse(
        final_url = final.get("final_url") or url,
        raw_last_url = final.get("raw_last_url") or url,
        captcha_detected = bool(final.get("captcha_detected")),
        screenshot_b64 = final.get("screenshot_b64"),
        attempts_made = attempt_made,
        nav_history = final.get("nav_history", []),
    )
    return JSONResponse(status_code=200, content=resp.dict())

# --- Simple HTML UI (embedded) - GET / returns this page ---
@app.get("/", response_class=HTMLResponse)
async def ui_index(request: Request):
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>GPLinks Bypass</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:16px;background:#f5f7fb}
    .card{max-width:900px;margin:18px auto;padding:20px;background:#fff;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,0.06)}
    input[type=text]{width:68%;padding:10px;border-radius:6px;border:1px solid #ddd}
    button{padding:10px 14px;border-radius:6px;border:none;background:#2563eb;color:#fff;cursor:pointer}
    label{margin-left:8px}
    pre{background:#f7f8fb;padding:12px;border-radius:6px;white-space:pre-wrap}
    img.debug{max-width:100%;margin-top:8px;border-radius:6px;border:1px solid #eee}
  </style>
</head>
<body>
  <div class="card">
    <h2>GPLinks Bypass — full flow</h2>
    <p>Paste GPLinks URL, check "Include screenshot" if you'd like a debug image. The service will follow the full flow (15s waits, 3 verify clicks, final Get Link click) and return the final destination.</p>
    <div>
      <input id="gplink" type="text" placeholder="https://gplinks.co/..." />
      <button onclick="startBypass()">Start</button>
    </div>
    <div style="margin-top:12px;">
      <label>Attempts: <input id="attempts" type="number" value="2" min="1" max="6" style="width:72px" /></label>
      <label style="margin-left:12px"><input id="headless" type="checkbox" checked/> Headless</label>
      <label style="margin-left:12px"><input id="screenshot" type="checkbox" /> Include screenshot</label>
      <label style="margin-left:12px">API Key: <input id="apikey" type="text" placeholder="(optional)" style="width:200px"/></label>
    </div>

    <div id="spinner" style="display:none;margin-top:12px">⏳ Bypassing — this may take 20–60s depending on flow...</div>
    <div id="output" style="margin-top:14px"></div>
  </div>

<script>
function escapeHtml(s){ if(!s) return ''; return s.replace(/'/g,"\\'").replace(/"/g,'\\"'); }

async function startBypass(){
  const url = document.getElementById('gplink').value.trim();
  if(!url){ alert('Please enter a GPLinks URL'); return; }
  const attempts = parseInt(document.getElementById('attempts').value || '2', 10);
  const headless = document.getElementById('headless').checked;
  const include_screenshot = document.getElementById('screenshot').checked;
  const apikey = document.getElementById('apikey').value.trim();

  document.getElementById('spinner').style.display = 'block';
  document.getElementById('output').innerHTML = '';

  try{
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
    let html = `<pre>✅ Final URL: ${data.final_url}\nAttempts: ${data.attempts_made}\nCaptcha Detected: ${data.captcha_detected}\nRaw Last URL: ${data.raw_last_url}\nNavigation history: ${JSON.stringify(data.nav_history || [])}</pre>`;
    html += `<p><button onclick="window.open('${escapeHtml(data.final_url)}','_blank')">Open final URL</button></p>`;
    if(data.screenshot_b64){
      html += `<p><a href="data:image/png;base64,${data.screenshot_b64}" download="screenshot.png">Download screenshot</a></p>`;
      html += `<p><img class="debug" src="data:image/png;base64,${data.screenshot_b64}" /></p>`;
    }
    document.getElementById('output').innerHTML = html;
  }catch(e){
    document.getElementById('output').innerText = 'Error: ' + e;
  }finally{
    document.getElementById('spinner').style.display = 'none';
  }
}
</script>
</body>
</html>
"""
    return HTMLResponse(content=html)

# --- health ---
@app.get("/health")
async def health():
    return {"status":"ok"}