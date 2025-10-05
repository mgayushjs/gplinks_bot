redirects and clicks. Returns dict with final_url, raw_last_url, captcha_detected, screenshot_b64 (maybe).
    """
    result = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None}
    screenshot_b64 = None
    screenshot_info = f"attempt_{attempt_num}.png"

    try:
        if progress_logger:
            progress_logger(f"[attempt {attempt_num}] goto {url}")
        try:
            await page.goto(url, timeout=NAV_TIMEOUT)
        except PlaywrightTimeoutError:
            logger.warning("page.goto timeout attempt %s", attempt_num)
        except Exception:
            logger.exception("page.goto error attempt %s", attempt_num)

        # explicit click on common redirect buttons early
        try:
            btn = await page.wait_for_selector("a#btn-main, button#btn-main, a[role='button'], a.btn", timeout=20000)
            if btn:
                try:
                    if progress_logger:
                        progress_logger(f"[attempt {attempt_num}] found redirect button - clicking")
                    await btn.click(timeout=CLICK_TIMEOUT)
                    await page.wait_for_timeout(1500)
                except Exception:
                    logger.debug("button click failed")
        except Exception:
            # no button found early - continue
            pass

        start = time.time()
        last_url = page.url

        while time.time() - start < MAX_TOTAL_WAIT:
            current_url = page.url
            result["raw_last_url"] = current_url

            # if host is get2.in try decode quickly
            decoded = try_decode_get2_in(current_url)
            if decoded:
                result["final_url"] = decoded
                if progress_logger:
                    progress_logger(f"[attempt {attempt_num}] decoded get2.in -> {decoded}")
                # optional screenshot
                try:
                    screenshot_b64 = await take_screenshot_base64(page)
                    result["screenshot_b64"] = screenshot_b64
                except Exception:
                    pass
                return result

            if looks_final(current_url) and current_url != url:
                result["final_url"] = current_url
                try:
                    screenshot_b64 = await take_screenshot_base64(page)
                    result["screenshot_b64"] = screenshot_b64
                    result["raw_last_url"] = current_url
                except Exception:
                    pass
                return result

            # check content for captcha
            content = ""
            try:
                content = await page.content()
            except Exception:
                content = ""

            if any(k in content.lower() for k in ("captcha", "recaptcha", "hcaptcha", "i am not a robot", "please verify")):
                result["captcha_detected"] = True
                try:
                    screenshot_b64 = await take_screenshot_base64(page)
                    result["screenshot_b64"] = screenshot_b64
                except Exception:
                    pass
                return result

            # try clicking known selectors
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
                            awai# web_bypass.py
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
    return {"status": "ok"}tion:
            logger.exception("Navigation error on attempt %d", attempt)

        # try explicit button click(s) often present on GPLinks
        try:
            btn = await page.wait_for_selector("a#btn-main, button#btn-main, a[role='button']", timeout=20000)
            if btn:
                try:
                    if progress_callback:
                        await progress_callback(f"[Attempt {attempt}] Found redirect button; clicking...")
                    await btn.click(timeout=click_timeout)
                    await page.wait_for_timeout(1800)
                except Exception:
                    logger.debug("Redirect button click failed on attempt %d", attempt)
        except Exception:
            pass

        def looks_final(u: str) -> bool:
            return "gplinks.co" not in (u or "").lower() and "gplinks" not in (u or "").lower()

        start = time.time()
        last_url = page.url

        while time.time() - start < MAX_TOTAL_WAIT:
            current_url = page.url
            result["raw_last_page_url"] = current_url

            if looks_final(current_url) and current_url != url:
                result["final_url"] = current_url
                try:
                    await page.screenshot(path=screenshot_path, full_page=True)
                    result["screenshot"] = screenshot_path
                except Exception:
                    pass
                return result

            try:
                content = await page.content()
            except Exception:
                content = ""

            # detect captcha-like content
            captcha_keywords = ["captcha", "recaptcha", "hcaptcha", "please verify", "i am not a robot"]
            if any(k in content.lower() for k in captcha_keywords):
                result["captcha_detected"] = True
                try:
                    await page.screenshot(path=screenshot_path, full_page=True)
                    result["screenshot"] = screenshot_path
                except Exception:
                    pass
                return result

            # click common selectors
            selectors = [
                "a#btn-main", "a[href*='redirect']", "a[href*='http']", "a.btn",
                "button#btn-main", "button", "input[type=submit]", "a[role='button']"
            ]
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        try:
                            await el.click(timeout=click_timeout)
                            await page.wait_for_timeout(random.randint(900, 1800))
                        except Exception:
                            pass
                except Exception:
                    pass

            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

            # sniff meta refresh
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

            # sniff js redirect
            try:
                js = re.search(r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', content, re.IGNORECASE)
                if js:
                    target = urljoin(page.url, js.group(1).strip())
                    try:
                        await page.goto(target, timeout=15000)
                        continue
                    except Exception:
                        pass
            except Exception:
                pass

            # external anchors
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
                                if looks_final(page.url):
                                    result["final_url"] = page.url
                                    try:
                                        await page.screenshot(path=screenshot_path, full_page=True)
                                        result["screenshot"] = screenshot_path
                                    except Exception:
                                        pass
                                    return result
                            except Exception:
                                return {"final_url": full, "screenshot": None, "captcha_detected": False, "raw_last_page_url": page.url}
                    except Exception:
                        pass
            except Exception:
                pass

            if page.url != last_url:
                last_url = page.url
            await page.wait_for_timeout(1000)

        # timeout reached
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            result["screenshot"] = screenshot_path
        except Exception:
            pass
        result["final_url"] = page.url
        return result

    except Exception:
        logger.exception("Unexpected error in attempt_bypass_once")
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            result["screenshot"] = screenshot_path
        except Exception:
            pass
        return result


# -----------------------------
# Top-level bypass with retries + human-like actions
# -----------------------------
async def bypass_gplinks(url: str, progress_callback=None) -> dict:
    attempts = int(CONFIG.get("RETRY_ATTEMPTS", 4))
    headless = bool(CONFIG.get("HEADLESS", True))
    simulate_mouse = bool(CONFIG.get("SIMULATE_MOUSE", True))

    logger.info("bypass_gplinks start: attempts=%s headless=%s simulate_mouse=%s url=%s", attempts, headless, simulate_mouse, url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36", java_script_enabled=True)
        page = await context.new_page()

        try:
            last_result = {"final_url": url, "screenshot": None, "captcha_detected": False, "raw_last_page_url": url}
            for attempt in range(1, attempts + 1):
                nav_timeout = int(BASE_NAV_TIMEOUT * (1 + 0.25 * (attempt - 1)))
                click_timeout = int(BASE_CLICK_TIMEOUT * (1 + 0.15 * (attempt - 1)))

                if progress_callback:
                    await progress_callback(f"Attempt {attempt}/{attempts} — nav_timeout={nav_timeout}ms")

                if simulate_mouse:
                    await do_human_like_actions(page, attempt)

                res = await attempt_bypass_once(page, url, nav_timeout, click_timeout, attempt, progress_callback=progress_callback)
                last_result = res

                if res.get("captcha_detected"):
                    if progress_callback:
                        await progress_callback("CAPTCHA detected — aborting attempts.")
                    return res

                def looks_final(u: str) -> bool:
                    return "gplinks.co" not in (u or "").lower() and "gplinks" not in (u or "").lower()

                if looks_final(res.get("final_url")) and res.get("final_url") != url:
                    if progress_callback:
                        await progress_callback(f"Success on attempt {attempt}: {res.get('final_url')}")
                    return res

                # backoff
                if attempt < attempts:
                    backoff = (2.0 ** attempt)
                    jitter = random.uniform(-1.5, 1.5)
                    wait_t = max(0.5, backoff + jitter)
                    if progress_callback:
                        await progress_callback(f"No final URL yet — waiting {wait_t:.1f}s before retry {attempt+1}...")
                    await page.wait_for_timeout(int(wait_t * 1000))
                    try:
                        await page.reload(timeout=5000)
                    except Exception:
                        pass

            if progress_callback:
                await progress_callback("All attempts done — returning best observed result.")
            return last_result

        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


# -----------------------------
# Command handlers and main handler
# -----------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.reply("👋 Hello! Send a GPLinks URL (https://gplinks.co/...) or reply to a message containing one. Use /status to see config.")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    lines = [f"{k}: {v}" for k, v in CONFIG.items()]
    await message.reply("Current settings:\n" + "\n".join(lines))


@dp.message(Command("set_headless"))
async def cmd_set_headless(message: Message):
    args = (message.text or "").split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        await message.reply("Usage: /set_headless on|off")
        return
    CONFIG["HEADLESS"] = args[1].lower() == "on"
    await message.reply(f"HEADLESS set to {CONFIG['HEADLESS']}")


@dp.message(Command("set_retries"))
async def cmd_set_retries(message: Message):
    args = (message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("Usage: /set_retries <number>")
        return
    CONFIG["RETRY_ATTEMPTS"] = max(1, int(args[1]))
    await message.reply(f"RETRY_ATTEMPTS set to {CONFIG['RETRY_ATTEMPTS']}")


@dp.message(Command("set_mouse"))
async def cmd_set_mouse(message: Message):
    args = (message.text or "").split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        await message.reply("Usage: /set_mouse on|off")
        return
    CONFIG["SIMULATE_MOUSE"] = args[1].lower() == "on"
    await message.reply(f"SIMULATE_MOUSE set to {CONFIG['SIMULATE_MOUSE']}")


@dp.message(AnyTextFilter())
async def handle_any_text(message: Message):
    text = (message.text or "").strip()

    # accept /bypass <url>
    if text.lower().startswith("/bypass"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("Usage: /bypass <gplinks_url>")
            return
        target = extract_url_from_text(parts[1]) or parts[1].strip()
    else:
        if message.reply_to_message and message.reply_to_message.text:
            target = extract_url_from_text(message.reply_to_message.text) or extract_url_from_text(text)
        else:
            target = extract_url_from_text(text)

    if not target:
        # ignore non-gplinks text to avoid spam
        # reply to greetings to help usability
        if text.lower().startswith("hi") or text.lower().startswith("hello"):
            await cmd_start(message)
        return

    progress_msg: Optional[Message] = None
    if True:
        progress_msg = await send_progress(chat_id=message.chat.id, text="⏳ Starting bypass...", reply_to_message_id=message.message_id)

    async def progress_cb(txt: str):
        nonlocal progress_msg
        logger.info("Progress: %s", txt)
        progress_msg = await send_progress(chat_id=message.chat.id, text=txt, edit_message=progress_msg)

    try:
        await progress_cb(f"Processing: {target}")
        result = await bypass_gplinks(target, progress_callback=progress_cb)
        final = result.get("final_url")
        captcha = result.get("captcha_detected", False)
        screenshot = result.get("screenshot")

        await progress_cb("Bypass finished — preparing results...")

        if captcha:
            await message.reply("⚠️ CAPTCHA detected — I cannot solve it automatically. Screenshot below for manual review.")
            if screenshot and os.path.exists(screenshot):
                await bot.send_photo(chat_id=message.chat.id, photo=FSInputFile(screenshot), caption="CAPTCHA screenshot")
            else:
                await message.reply("Could not capture screenshot.")
            await message.reply(f"Partial page URL: {result.get('raw_last_page_url')}")
            return

        await message.reply(f"✅ Final URL (best-effort):\n{final}")
        if screenshot and os.path.exists(screenshot):
            await bot.send_photo(chat_id=message.chat.id, photo=FSInputFile(screenshot), caption="Debug screenshot")

    except Exception as exc:
        logger.exception("Error during bypass flow")
        # notify admin (optional)
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=f"Bot error: {exc}")
            except Exception:
                pass
        await message.reply(f"⚠️ Error while bypassing: {exc}")


# -----------------------------
# Startup & main
# -----------------------------
async def on_startup_notify():
    msg = "Bot started and polling for messages."
    logger.info(msg)
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=msg)
        except Exception:
            logger.exception("Failed to notify admin on startup")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await on_startup_notify()
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:

        logger.exception("Fatal error in main loop")

