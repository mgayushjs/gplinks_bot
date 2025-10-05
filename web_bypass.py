# web_bypass.py
"""
Arolinks bypass service (single-file).
UI: GET  /          -> HTML page (JS posts JSON to /bypass)
API: POST /bypass   -> { url, attempts, headless, include_screenshot }
Health: GET /health -> {"status":"ok"}

Behavior:
 - Open the provided Arolinks URL
 - Wait for timers/redirects and attempt verification clicks
 - Capture network responses and script-injected links
 - Click "Get Link"/"Continue" style elements when they appear
 - Return the final destination URL (avoid returning intermediate ad pages)
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("arolinks-bypass")

app = FastAPI(title="Arolinks Bypass (embedded UI)")

# ---------- Config ----------
VERIFY_WAIT_SECONDS = 5        # base wait; many arolinks flows use short timers — we'll check repeatedly
MAX_VERIFY_ROUNDS = 6         # maximum repeated verify/click rounds before giving up
CLICK_TIMEOUT = 12_000        # ms
NAV_TIMEOUT = 60_000          # ms
MAX_TOTAL_WAIT = 90          # seconds per attempt to reach final
DEFAULT_ATTEMPTS = 3
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
API_KEY = os.environ.get("API_KEY")  # optional

# ---------- Request/Response models ----------
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
    note: Optional[str] = None

# ---------- Helpers ----------
def is_arolinks(u: Optional[str]) -> bool:
    if not u:
        return False
    return "arolinks" in u.lower()

def looks_final(u: Optional[str]) -> bool:
    if not u:
        return False
    s = u.lower()
    # final if not an arolinks shortener or obvious known intermediates
    return ("arolinks" not in s) and ("ad" not in s and "procinehub" not in s and "ads" not in s)

def resolve_href(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except Exception:
        return href or ""

async def take_screenshot_b64(page: Page) -> str:
    b = await page.screenshot(full_page=True)
    return base64.b64encode(b).decode()

# Patterns to find "continue/get" buttons
ACTION_PATTERNS = [
    "continue", "get link", "get-link", "getlink", "click here", "open link", "show link", "proceed",
    "verification", "verify", "download", "generate"
]

# CSS selector candidates
CANDIDATE_SELECTORS = [
    "a", "button", "input[type=button]", "input[type=submit]", "div[role='button']"
]

# ---------- Network response listener factory ----------
def make_response_listener(found: Set[str]):
    async def on_response(resp: Response):
        try:
            u = resp.url
            if not u:
                return
            if "arolinks" in u.lower():
                return
            # capture responses containing likely final targets
            if any(k in u.lower() for k in ("generate?code=", "drive.google.com", "googleusercontent", "dl.dropboxusercontent", "herokuapp.com", "cdn.discordapp.com")):
                found.add(u)
                return
            # best-effort: examine short text/json responses for final urls
            ct = resp.headers.get("content-type", "")
            if ("text" in ct or "json" in ct) and len(u) < 500:
                try:
                    txt = await resp.text()
                    m = re.search(r'https?://[^\s"\'<>]+/(?:generate\?code=|drive|googleusercontent|dropboxusercontent|discordapp|file|download)[^\s"\'<>]*', txt, re.IGNORECASE)
                    if m:
                        found.add(m.group(0))
                except Exception:
                    pass
        except Exception:
            pass
    return on_response

# ---------- Core: attempt that follows the arolinks flow ----------
async def attempt_follow_arolinks(page: Page, start_url: str, progress_logger=None):
    """
    Try to resolve the arolinks flow:
      - open start_url
      - repeatedly wait and try clicking 'verify/continue' style elements
      - capture network responses and DOM-injected links
      - click final Get/Continue link and return final destination
    """
    result = {"final_url": start_url, "raw_last_url": start_url, "captcha_detected": False, "screenshot_b64": None, "nav_history": []}
    found_network_urls: Set[str] = set()
    listener = make_response_listener(found_network_urls)
    page.on("response", listener)

    try:
        if progress_logger:
            progress_logger(f"goto {start_url}")
        try:
            await page.goto(start_url, timeout=NAV_TIMEOUT)
        except PlaywrightTimeoutError:
            if progress_logger:
                progress_logger("initial goto timeout")
        except Exception as e:
            logger.exception("goto error: %s", e)

        result["nav_history"].append(page.url)

        start_time = time.time()
        rounds = 0

        # We'll iterate: wait short amount, try clicks, scan content/network, repeat up to MAX_VERIFY_ROUNDS
        while time.time() - start_time < MAX_TOTAL_WAIT and rounds < MAX_VERIFY_ROUNDS:
            rounds += 1
            if progress_logger:
                progress_logger(f"round {rounds}: wait {VERIFY_WAIT_SECONDS}s then scan/click")
            # wait for timer / ad flow to progress
            await page.wait_for_timeout(VERIFY_WAIT_SECONDS * 1000)

            # First, check network-discovered URLs (best candidate)
            if found_network_urls:
                chosen = sorted(found_network_urls)[0]
                if progress_logger:
                    progress_logger(f"network discovered candidate {chosen}")
                # navigate to it (or return it if looks final)
                if looks_final(chosen):
                    result["final_url"] = chosen
                    result["raw_last_url"] = page.url
                    try:
                        result["screenshot_b64"] = await take_screenshot_b64(page)
                    except Exception:
                        pass
                    return result
                else:
                    # attempt to navigate to the candidate to see if it resolves further
                    try:
                        await page.goto(chosen, timeout=15000)
                        result["nav_history"].append(page.url)
                    except Exception:
                        pass

            # Next, try clicking visible elements that have action-like text
            clicked = False
            for sel in CANDIDATE_SELECTORS:
                try:
                    elements = await page.query_selector_all(sel)
                except Exception:
                    elements = []
                for el in elements:
                    try:
                        # get visible text / aria-label / title
                        txt = ""
                        aria = ""
                        title = ""
                        try:
                            txt = (await el.inner_text() or "").strip().lower()
                        except Exception:
                            txt = ""
                        try:
                            aria = (await el.get_attribute("aria-label") or "").strip().lower()
                        except Exception:
                            aria = ""
                        try:
                            title = (await el.get_attribute("title") or "").strip().lower()
                        except Exception:
                            title = ""
                        combined = " ".join([txt, aria, title]).strip()
                        # skip empty elements
                        if not combined:
                            # small heuristic: if element has href attribute, consider it
                            try:
                                href = await el.get_attribute("href")
                                if href and ("http" in href or href.startswith("/")):
                                    combined = href.lower()
                            except Exception:
                                pass
                        # if combined contains any action pattern, try clicking
                        if any(p in combined for p in ACTION_PATTERNS) or ("getlink" in combined) or ("get-link" in combined):
                            try:
                                if progress_logger:
                                    progress_logger(f"Clicking element with text '{combined[:80]}'")
                                # Try click
                                await el.click(timeout=CLICK_TIMEOUT)
                                clicked = True
                                # wait a bit for network / nav
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=4000)
                                except Exception:
                                    await page.wait_for_timeout(1200)
                                result["nav_history"].append(page.url)
                                # if navigation reached a final-looking url, return it
                                if looks_final(page.url):
                                    result["final_url"] = page.url
                                    result["raw_last_url"] = page.url
                                    try:
                                        result["screenshot_b64"] = await take_screenshot_b64(page)
                                    except Exception:
                                        pass
                                    return result
                            except Exception:
                                # fallback evaluate click
                                try:
                                    await page.evaluate("(e)=>e.click()", el)
                                    clicked = True
                                    await page.wait_for_timeout(1200)
                                    result["nav_history"].append(page.url)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                if clicked:
                    break

            # After clicks, check page content for direct final links injected into page
            try:
                content = await page.content()
                # search for final link patterns (drive, googleusercontent, heroku, dropbox, discord CDN, direct file)
                m = re.search(r'https?://[^\s"\'<>]+/(?:generate\?code=|drive|googleusercontent|dropboxusercontent|discordapp|cdn\.|file|download)[^\s"\'<>]*', content, re.IGNORECASE)
                if m:
                    candidate = m.group(0)
                    if progress_logger:
                        progress_logger(f"Found candidate in page content: {candidate}")
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

            # detect captcha-like content
            try:
                txt = await page.content()
            except Exception:
                txt = ""
            if any(k in txt.lower() for k in ("captcha", "recaptcha", "hcaptcha", "i am not a robot", "please verify")):
                result["captcha_detected"] = True
                result["raw_last_url"] = page.url
                try:
                    result["screenshot_b64"] = await take_screenshot_b64(page)
                except Exception:
                    pass
                return result

            # If nothing actionable yet, try a gentle navigation of anchors to escape intermediates like procinehub
            cur = page.url or ""
            if ("procinehub" in cur.lower() or "ad" in cur.lower()) and not looks_final(cur):
                try:
                    anchors = await page.query_selector_all("a[href]")
                    for a in anchors:
                        try:
                            href = await a.get_attribute("href")
                            if not href:
                                continue
                            full = resolve_href(page.url, href)
                            if looks_final(full):
                                if progress_logger:
                                    progress_logger(f"Following anchor to candidate final {full}")
                                try:
                                    await page.goto(full, timeout=15000)
                                    result["nav_history"].append(page.url)
                                    if looks_final(page.url):
                                        result["final_url"] = page.url
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
                except Exception:
                    pass

            # small sleep and continue next round
            await page.wait_for_timeout(1000)

        # end rounds: fallback - if found network url choose first non-arolinks
        if found_network_urls:
            for candidate in sorted(found_network_urls):
                if looks_final(candidate):
                    result["final_url"] = candidate
                    result["raw_last_url"] = page.url
                    try:
                        result["screenshot_b64"] = await take_screenshot_b64(page)
                    except Exception:
                        pass
                    return result

        # final fallback: try anchors on page to find external link
        try:
            anchors = await page.query_selector_all("a[href]")
            for a in anchors:
                try:
                    href = await a.get_attribute("href")
                    if not href:
                        continue
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
        except Exception:
            pass

        # give up: return last page.url
        result["final_url"] = page.url or start_url
        result["raw_last_url"] = page.url or start_url
        try:
            result["screenshot_b64"] = await take_screenshot_b64(page)
        except Exception:
            pass
        return result

    except Exception:
        logger.exception("attempt_follow_arolinks error")
        result["final_url"] = start_url
        result["raw_last_url"] = start_url
        try:
            result["screenshot_b64"] = await take_screenshot_b64(page)
        except Exception:
            pass
        return result
    finally:
        try:
            page.remove_listener("response", listener)
        except Exception:
            pass

# ---------- POST /bypass endpoint ----------
@app.post("/bypass")
async def bypass_endpoint(req: BypassRequest, x_api_key: Optional[str] = Header(None)):
    # optional API key check
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Missing/invalid API key")

    url = str(req.url)
    attempts = max(1, min(6, int(req.attempts or DEFAULT_ATTEMPTS)))
    headless = bool(req.headless)
    include_screenshot = bool(req.include_screenshot)

    logger.info("Arolinks bypass request: url=%s attempts=%s headless=%s", url, attempts, headless)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        final = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None, "nav_history": []}
        attempt_made = 0
        try:
            for i in range(1, attempts + 1):
                attempt_made = i
                # tiny human-like move
                try:
                    await page.mouse.move(100, 100)
                except Exception:
                    pass

                res = await attempt_follow_arolinks(page, url, progress_logger=lambda t: logger.info("BYPASS: %s", t))
                final.update(res)
                final["nav_history"] = res.get("nav_history", [])

                # if final looks final (not arolinks) done
                if looks_final(final.get("final_url")):
                    break

                # else small backoff and reload
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

    resp = BypassResponse(
        final_url = final.get("final_url") or url,
        raw_last_url = final.get("raw_last_url") or url,
        captcha_detected = bool(final.get("captcha_detected")),
        screenshot_b64 = final.get("screenshot_b64"),
        attempts_made = attempt_made,
        nav_history = final.get("nav_history", []),
    )
    return JSONResponse(status_code=200, content=resp.dict())

# ---------- Embedded UI (no templates) ----------
@app.get("/", response_class=HTMLResponse)
async def ui_index(request: Request):
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Arolinks Bypass</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:16px;background:#f5f7fb}
    .card{max-width:900px;margin:18px auto;padding:20px;background:#fff;border-radius:10px}
    input[type=tex