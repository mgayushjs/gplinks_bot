# web_bypass.py
"""
GPLinks follower service.
- Open provided GPLinks URL.
- Follow redirects / clicks / network responses until navigation stabilizes or safety limits reached.
- Return the last visited URL (final_url), optionally a screenshot and captcha detection.
Endpoints:
  GET  /       -> simple UI
  POST /bypass -> JSON API: { "url": "...", "attempts": 3, "headless": true, "include_screenshot": true }
  GET  /health -> {"status":"ok"}
"""
import os
import re
import time
import base64
import logging
from typing import Optional, List, Set
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, AnyHttpUrl
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web-bypass")

app = FastAPI(title="GPLinks Follower - returns last opened link")

# ====== Config ======
DEFAULT_HEADLESS = True
NAV_TIMEOUT = 60_000         # ms for goto
CLICK_TIMEOUT = 12_000       # ms for clicks
WAIT_AFTER_OPEN = 5         # seconds to wait after opening the initial GPLinks
MAX_TOTAL_WAIT = 90         # seconds per attempt to follow navigation
DEFAULT_ATTEMPTS = 3
MAX_NAV_HISTORY = 30        # safety cap for navigations
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"

API_KEY = os.environ.get("API_KEY")

# ====== Request/Response models ======
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

# ====== Helpers ======
def looks_shortener(u: Optional[str]) -> bool:
    if not u:
        return False
    s = u.lower()
    return "gplinks" in s or "get2.in" in s or "short" in s  # conservative

def looks_final(u: Optional[str]) -> bool:
    if not u:
        return False
    s = u.lower()
    # treat URL as "final" when it doesn't look like gplinks/get2 or common shortener
    return ("gplinks.co" not in s) and ("get2.in" not in s) and ("gplinks" not in s)

def resolve_href(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except Exception:
        return href

async def take_screenshot_b64(page: Page) -> str:
    b = await page.screenshot(full_page=True)
    return base64.b64encode(b).decode()

# ====== Smart click helper: clicks "Get Link" like elements ======
async def try_click_getlink_elements(page: Page, logger_fn=None) -> Optional[str]:
    """
    Find and click elements that look like "Get Link" / "Get Link" actions.
    Return a final URL if clicking caused navigation or href looks final.
    """
    patterns = ["get link", "get-link", "getlink", "get now", "show link", "click here",
                "continue", "open link", "get url", "get code", "generate", "download"]

    try:
        els = await page.query_selector_all("a, button, input[type=button], input[type=submit]")
    except Exception:
        els = []

    base_url = page.url
    for el in els:
        try:
            text = ""
            aria = ""
            href = None
            try:
                text = (await el.inner_text() or "").strip().lower()
            except Exception:
                text = ""
            try:
                aria = (await el.get_attribute("aria-label") or "").strip().lower()
            except Exception:
                aria = ""
            try:
                href = await el.get_attribute("href")
            except Exception:
                href = None

            combined = f"{text} {aria}".strip()
            if any(p in combined for p in patterns):
                if logger_fn:
                    logger_fn(f"click candidate: text='{combined[:60]}' href={href}")
                # click attempt
                try:
                    await el.click(timeout=CLICK_TIMEOUT)
                except Exception:
                    # fallback using evaluate
                    try:
                        await page.evaluate("(e)=>e.click()", el)
                    except Exception:
                        pass
                # allow some time for navigation/xhr
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    await page.wait_for_timeout(1200)

                # if navigation happened:
                new_url = page.url
                if new_url and new_url != base_url:
                    # try to prefer a heroku-like generate if present in page HTML
                    try:
                        txt = await page.content()
                        m = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', txt)
                        if m:
                            return m.group(0)
                    except Exception:
                        pass
                    return new_url

                # if no navigation, but href looks final, return it
                if href:
                    full = resolve_href(base_url, href)
                    if looks_final(full) or "herokuapp.com" in full or "/generate?code=" in full:
                        return full

                # check page for heroku in case it's been injected
                try:
                    txt = await page.content()
                    m2 = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', txt)
                    if m2:
                        return m2.group(0)
                except Exception:
                    pass
        except Exception:
            pass
    return None

# ====== Network listener helper (optional, used to capture interesting responses) ======
def make_response_listener(found_set: Set[str]):
    async def on_response(resp: Response):
        try:
            u = resp.url
            if "herokuapp.com" in u or "/generate?code=" in u:
                found_set.add(u)
            # small, best-effort body check for interesting urls if content-type text/json
            try:
                ct = resp.headers.get("content-type", "")
                if ("json" in ct or "text" in ct) and len(u) < 400:
                    txt = await resp.text()
                    m = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', txt)
                    if m:
                        found_set.add(m.group(0))
            except Exception:
                pass
        except Exception:
            pass
    return on_response

# ====== Main single attempt: follow navigation chain, clicking 'get' buttons etc ======
async def bypass_once(page: Page, url: str, attempt_num: int, logger_fn=None):
    result = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None}
    nav_history: List[str] = []
    found_network_urls: Set[str] = set()

    # register response listener
    listener = make_response_listener(found_network_urls)
    page.on("response", listener)

    try:
        if logger_fn:
            logger_fn(f"[attempt {attempt_num}] goto {url}")
        try:
            await page.goto(url, timeout=NAV_TIMEOUT)
        except PlaywrightTimeoutError:
            if logger_fn:
                logger_fn("page.goto timeout")
        except Exception as e:
            if logger_fn:
                logger_fn(f"page.goto exception: {e}")

        # initial wait (you requested 5s)
        try:
            await page.wait_for_timeout(WAIT_AFTER_OPEN * 1000)
        except Exception:
            pass

        # record initial url
        nav_history.append(page.url)

        start_time = time.time()
        last_url = page.url

        # small helper to append to history (guard duplicates)
        def push_history(u: str):
            if not u:
                return
            if not nav_history or nav_history[-1] != u:
                nav_history.append(u)

        # main follow loop
        while time.time() - start_time < MAX_TOTAL_WAIT and len(nav_history) < MAX_NAV_HISTORY:
            current_url = page.url
            result["raw_last_url"] = current_url

            # if network found an interesting URL, prefer it immediately
            if found_network_urls:
                chosen = sorted(found_network_urls)[0]
                result["final_url"] = chosen
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                result["raw_last_url"] = current_url
                result["nav_history"] = nav_history
                return result

            # try clicking get-link elements (primary)
            click_res = await try_click_getlink_elements(page, logger_fn=logger_fn)
            if click_res:
                push_history(page.url)
                result["final_url"] = click_res
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                result["raw_last_url"] = page.url
                result["nav_history"] = nav_history
                return result

            # update history if URL changed
            if page.url != last_url:
                last_url = page.url
                push_history(last_url)

            # look for heroku direct in page content
            try:
                cont = await page.content()
                m = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', cont)
                if m:
                    result["final_url"] = m.group(0)
                    try:
                        result["screenshot_b64"] = await take_screenshot_b64(page)
                    except Exception:
                        pass
                    result["raw_last_url"] = page.url
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
                result["raw_last_url"] = page.url
                result["nav_history"] = nav_history
                return result

            # if the page looks like it left the shortener domain, give JS a short extra window to produce final links
            if looks_final(current_url) and current_url != url:
                # attempt aggressive clicks & scans for a few seconds
                extra_end = time.time() + 8
                while time.time() < extra_end:
                    # network-first check
                    if found_network_urls:
                        chosen = sorted(found_network_urls)[0]
                        result["final_url"] = chosen
                        try:
                            result["screenshot_b64"] = await take_screenshot_b64(page)
                        except Exception:
                            pass
                        result["raw_last_url"] = page.url
                        result["nav_history"] = nav_history
                        return result

                    click_res = await try_click_getlink_elements(page, logger_fn=logger_fn)
                    if click_res:
                        push_history(page.url)
                        result["final_url"] = click_res
                        try:
                            result["screenshot_b64"] = await take_screenshot_b64(page)
                        except Exception:
                            pass
                        result["raw_last_url"] = page.url
                        result["nav_history"] = nav_history
                        return result

                    # scan scripts and innerText for obfuscated heroku links or base64
                    try:
                        scripts = await page.query_selector_all("script")
                        for s in scripts:
                            try:
                                txt = await s.inner_text()
                                if not txt:
                                    continue
                                m2 = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s"\'<>]*generate\?code=[^"&\'<>]+', txt)
                                if m2:
                                    result["final_url"] = m2.group(0)
                                    try:
                                        result["screenshot_b64"] = await take_screenshot_b64(page)
                                    except Exception:
                                        pass
                                    result["raw_last_url"] = page.url
                                    result["nav_history"] = nav_history
                                    return result
                                # try base64 decoding candidates
                                b64s = re.findall(r'([A-Za-z0-9_\-]{20,}={0,2})', txt)
                                for cand in b64s:
                                    try:
                                        padding = (-len(cand)) % 4
                                        bs = cand.encode()
                                        if padding:
                                            bs += b"=" * padding
                                        dec = base64.urlsafe_b64decode(bs).decode(errors="ignore")
                                        if "herokuapp.com" in dec and "generate" in dec:
                                            result["final_url"] = dec
                                            try:
                                                result["screenshot_b64"] = await take_screenshot_b64(page)
                                            except Exception:
                                                pass
                                            result["raw_last_url"] = page.url
                                            result["nav_history"] = nav_history
                                            return result
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    except Exception:
                        pass

                    try:
                        txt = await page.evaluate("() => (document.body.innerText || '')")
                        if txt:
                            m3 = re.search(r'https?://[A-Za-z0-9\-.]+herokuapp\.com/[^\s]+generate\?code=[A-Za-z0-9_\-]+', txt)
                            if m3:
                                result["final_url"] = m3.group(0)
                                try:
                                    result["screenshot_b64"] = await take_screenshot_b64(page)
                                except Exception:
                                    pass
                                result["raw_last_url"] = page.url
                                result["nav_history"] = nav_history
                                return result
                    except Exception:
                        pass

                    await page.wait_for_timeout(1000)

                # after extra window, accept current_url as final if nothing found
                result["final_url"] = page.url
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                result["raw_last_url"] = page.url
                result["nav_history"] = nav_history
                return result

            # generic fallback clicks to progress the flow
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

            # small wait before next loop
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass

            if page.url != last_url:
                last_url = page.url
                push_history(last_url)

            await page.wait_for_timeout(800)

        # loop finished: best effort return last url
        result["final_url"] = page.url or result["final_url"]
        try:
            result["screenshot_b64"] = await take_screenshot_b64(page)
        except Exception:
            pass
        result["raw_last_url"] = page.url
        result["nav_history"] = nav_history
        return result

    except Exception:
        logger.exception("Error in bypass_once")
        result["nav_history"] = nav_history
        return result
    finally:
        try:
            page.remove_listener("response", listener)
        except Exception:
            pass

# ====== API endpoint ======
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
                    await page.mouse.move(120, 120)
                except Exception:
                    pass

                res = await bypass_once(page, url, i, logger_fn=lambda t: logger.info("BYPASS: %s", t))
                final.update(res)
                # stop on captcha
                if res.get("captcha_detected"):
                    break
                # if final looks final and not a shortener, stop
                if looks_final(final.get("final_url")) and "gplinks" not in (final.get("final_url") or "").lower():
                    break
                # else retry
                if i < attempts:
                    backoff = (2 ** i) + 0.5 * i
                    logger.i    raw_response_url: str
    attempts_made: int
    note: Optional[str] = None

# ---------- Helpers ----------
INTERMEDIATE_HOSTS = ("procinehub.com", "ad.", "ads.", "short", "interstitial")

def looks_intermediate(url: Optional[str]) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(h in u for h in INTERMEDIATE_HOSTS)

def resolve_meta_or_js_redirect(html: str, base: str) -> Optional[str]:
    """Try to extract a redirect URL from meta refresh or simple JS assignments in HTML."""
    # meta refresh
    m = re.search(r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]*content=["\']?([^"\'>]+)["\']?', html, re.IGNORECASE)
    if m:
        # content like "5; url=https://example.com"
        mm = re.search(r'url=([^;\'"]+)', m.group(1), re.IGNORECASE)
        if mm:
            return urljoin(base, mm.group(1).strip())
    # simple JS window.location / location.href assignments
    m2 = re.search(r'window\.location(?:\.href)?\s*=\s*[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
    if m2:
        return urljoin(base, m2.group(1))
    # anchor with obvious final links in body text
    m3 = re.search(r'href=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
    if m3:
        return urljoin(base, m3.group(1))
    return None

# ---------- Core synchronous bypass function (runs in thread) ----------
def gplinks_bypass_sync(url: str, wait_seconds: int = DEFAULT_WAIT_SECONDS):
    """
    Use cloudscraper to perform the GPLinks flow synchronously.
    Returns final_url (string) or raises exception.
    """
    logger.info("Starting bypass_sync for %s", url)
    client = cloudscraper.create_scraper(allow_brotli=True, browser={'custom': USER_AGENT})
    client.headers.update({"User-Agent": USER_AGENT, "Referer": url})

    # 1) GET initial page
    try:
        r = client.get(url, allow_redirects=True, timeout=30)
    except Exception as e:
        logger.exception("Initial GET failed")
        raise RuntimeError(f"Initial GET failed: {e}")

    # Keep raw response URL for debugging
    raw_url = r.url

    # 2) parse for form#go-link
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", id="go-link")
    # Some variants may have id "go-link" on a DIV or the form could be missing; try fallback searching for form[action*='links/go']
    if not form:
        form = soup.find("form", attrs={"action": re.compile(r"links/go", re.IGNORECASE)})
    if not form:
        # If form not found, maybe the page uses a JS-built flow — try to sleep and re-get or return raw location if present
        logger.debug("form#go-link not found on initial page")
        # try to detect immediate JSON redirect or direct link in body
        candidate = None
        candidate = resolve_meta_or_js_redirect(r.text, r.url)
        if candidate:
            return candidate, raw_url, "extracted redirect from page"
        raise RuntimeError("Could not find go-link form on page; bypass not possible with this method")

    # collect input name/value pairs
    inputs = form.find_all("input")
    data = {}
    for inp in inputs:
        name = inp.get("name")
        if not name:
            continue
        value = inp.get("value", "")
        data[name] = value

    # wait (simulate user)
    logger.info("Waiting %ds before submitting form (simulate timer)", wait_seconds)
    time.sleep(max(0, int(wait_seconds)))

    # Prepare headers for AJAX POST
    headers = {
        "x-requested-with": "XMLHttpRequest",
        "referer": url,
        "User-Agent": USER_AGENT,
    }

    # Attempt POST to /links/go (domain same as initial)
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    post_url = urljoin(domain, "/links/go")

    try:
        post_resp = client.post(post_url, data=data, headers=headers, allow_redirects=False, timeout=30)
    except Exception as e:
        logger.exception("POST to links/go failed")
        raise RuntimeError(f"POST to links/go failed: {e}")

    # Many implementations return JSON with {"url": "..."}
    final_candidate = None
    note = None
    try:
        j = post_resp.json()
        final_candidate = j.get("url") or j.get("redirect") or j.get("data") or None
        note = "from /links/go json"
    except Exception:
        # Not JSON — maybe a redirect or HTML response
        # if Location header present
        loc = post_resp.headers.get("Location")
        if loc:
            final_candidate = urljoin(post_resp.url, loc)
            note = "from Location header"
        else:
            # try parsing text for link
            text = post_resp.text or ""
            cand = resolve_meta_or_js_redirect(text, post_resp.url)
            if cand:
                final_candidate = cand
                note = "from post response body"
            else:
                # fallback: maybe the original domain returned JSON in r.text earlier; try searching
                m = re.search(r'https?://[^\s"\'<>]+', post_resp.text or "")
                if m:
                    final_candidate = m.group(0)
                    note = "from text search"

    if not final_candidate:
        raise RuntimeError("Could not extract final URL from POST response")

    # If candidate is an intermediate host (procinehub or similar), try to follow it once to find a redirect/final url
    if looks_intermediate(final_candidate):
        logger.info("Final candidate appears intermediate (%s). Following it once to try to find final.", final_candidate)
        try:
            follow = client.get(final_candidate, allow_redirects=True, timeout=30)
            # if final redirected to new location, follow.url will be the final
            follow_url = follow.url
            # check meta/js in the page for final
            meta = resolve_meta_or_js_redirect(follow.text or "", follow_url)
            if meta and meta != follow_url:
                return meta, raw_url, "followed intermediate -> meta/js"
            # otherwise if follow_url seems final (not intermediate) return it
            if not looks_intermediate(follow_url):
                return follow_url, raw_url, "followed intermediate -> final"
        except Exception:
            logger.exception("Following intermediate candidate failed")
            # give up on follow

    return final_candidate, raw_url, note or "extracted"

# ---------- Async wrapper endpoint ----------
@app.post("/bypass")
async def bypass(req: BypassRequest):
    url = str(req.url)
    attempts = max(1, min(6, int(req.attempts or DEFAULT_ATTEMPTS)))
    wait_seconds = max(0, int(req.wait_seconds or DEFAULT_WAIT_SECONDS))

    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            final, raw, note = await asyncio.to_thread(gplinks_bypass_sync, url, wait_seconds)
            # ensure we return absolute url string
            if final and isinstance(final, str):
                # some short responses may be relative - resolve
                final_url = final
                # final sanity: if looks_intermediate and we have more attempts, retry once
                if looks_intermediate(final_url) and attempt < attempts:
                    logger.info("Attempt %d returned intermediate '%s' — retrying (attempt %d/%d)", attempt, final_url, attempt+1, attempts)
                    last_err = f"intermediate:{final_url}"
                    continue
                resp = BypassResponse(final_url=final_url, raw_response_url=raw, attempts_made=attempt, note=str(note))
                return JSONResponse(status_code=200, content=resp.dict())
            else:
                last_err = "no final url extracted"
        except Exception as e:
            logger.exception("Bypass attempt %d failed", attempt)
            last_err = str(e)
            # small backoff before retry
            await asyncio.sleep(1 + attempt)

    raise HTTPException(status_code=502, detail=f"Bypass failed after {attempts} attempts: {last_err}")

# ---------- Simple embedded UI ----------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    html = r'''
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>GPLinks Bypass (cloudscraper)</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:18px;background:#f5f7fb}
    .card{max-width:880px;margin:18px auto;padding:20px;background:#fff;border-radius:10px}
    input[type=text]{width:66%;padding:10px;border-radius:6px;border:1px solid #ddd}
    button{padding:9px 12px;border-radius:6px;border:none;background:#2563eb;color:#fff;cursor:pointer}
    pre{background:#f7f8fb;padding:12px;border-radius:6px;white-space:pre-wrap}
  </style>
</head>
<body>
  <div class="card">
    <h2>GPLinks Bypass (cloudscraper)</h2>
    <p>Paste GPLinks URL (or similar) and press Start. This tool will wait the timer, submit the hidden form and return the final URL.</p>
    <div>
      <input id="gplink" type="text" placeholder="https://gplinks.co/XXXX" />
      <button onclick="start()">Start</button>
    </div>
    <div style="margin-top:12px;">
      <label>Attempts: <input id="attempts" type="number" value="3" min="1" max="6" style="width:72px" /></label>
      <label style="margin-left:12px">Wait seconds: <input id="wait" type="number" value="15" min="0" max="60" style="width:72px" /></label>
    </div>
    <div id="spinner" style="display:none;margin-top:12px">⏳ Bypassing — please wait...</div>
    <div id="output" style="margin-top:14px"></div>
  </div>

<script>
async function start(){
  const url = document.getElementById('gplink').value.trim();
  if(!url){ alert('Enter a GPLinks URL'); return; }
  const attempts = parseInt(document.getElementById('attempts').value || '3', 10);
  const wait = parseInt(document.getElementById('wait').value || '15', 10);
  document.getElementById('spinner').style.display = 'block';
  document.getElementById('output').innerHTML = '';
  try{
    const res = await fetch('/bypass', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ url: url, attempts: attempts, wait_seconds: wait })
    });
    const data = await res.json();
    if(!res.ok){
      document.getElementById('output').innerText = 'Error: ' + (data.detail || JSON.stringify(data));
    } else {
      let html = `<pre>✅ Final URL: ${data.final_url}\nRaw response URL: ${data.raw_response_url}\nAttempts used: ${data.attempts_made}\nNote: ${data.note || ''}</pre>`;
      html += `<p><button onclick="window.open('${data.final_url}','_blank')">Open final URL</button></p>`;
      document.getElementById('output').innerHTML = html;
    }
  }catch(e){
    document.getElementById('output').innerText = 'Error: ' + e;
  }finally{
    document.getElementById('spinner').style.display = 'none';
  }
}
</script>
</body>
</html>
'''
    return HTMLResponse(content=html)

# ---------- Health ----------
@app.get("/health")
async def health():

    return {"status": "ok"}
