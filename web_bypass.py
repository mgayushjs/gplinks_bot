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

from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel, AnyHttpUrl
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web-bypass")

app = FastAPI(title="GPLinks Bypass Service")

# === Config (tweak if needed) ===
DEFAULT_HEADLESS = True
NAV_TIMEOUT = 60_000         # ms
CLICK_TIMEOUT = 12_000       # ms
MAX_TOTAL_WAIT = 60          # seconds per attempt
DEFAULT_ATTEMPTS = 3
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"

# optional API key (set as Railway/GitHub env var API_KEY)
API_KEY = None
try:
    import os
    API_KEY = os.environ.get("API_KEY")
except Exception:
    API_KEY = None

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
    """
    decode get2.in style encoded targets (base64/url-encoded). Returns decoded URL or None.
    """
    try:
        parsed = urlparse(url)
        if "get2.in" not in parsed.netloc.lower():
            return None
        query = parsed.query or parsed.path.split("?", 1)[-1]
        if not query:
            return None
        # try raw query parts
        candidate = None
        if "=" in query:
            parts = query.split("&")
            # take first v-like value that is long
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
        # try base64 urlsafe
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

        # try early button click (common on gplinks)
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

            # try decode get2.in quickly
            decoded = try_decode_get2_in(current_url)
            if decoded:
                result["final_url"] = decoded
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
                return result

            # try clicking common selectors
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

            # wait briefly for network changes
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass

            # sniff meta refresh or JS redirect in page content
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

            # anchors -> follow external anchors as best-effort
            try:
                anchors = await page.query_selector_all("a")
                for a in anchors:
                    try:
                        href = await a.get_attribute("href")
                        if not href or href.startswith("javascript:") or href.startswith("#"):
                            continue
                        full = urljoin(page.url, href)
                        if looks_final(full):
                            try:
                                await page.goto(full, timeout=15000)
                                if looks_final(page.url):
                                    result["final_url"] = page.url
                                    try:
                                        result["screenshot_b64"] = await take_screenshot_b64(page)
                                    except Exception:
                                        pass
                                    return result
                            except Exception:
                                result["final_url"] = full
                                return result
                    except Exception:
                        pass
            except Exception:
                pass

            if page.url != last_url:
                last_url = page.url
            await page.wait_for_timeout(1000)

        # timeout reached - best-effort
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