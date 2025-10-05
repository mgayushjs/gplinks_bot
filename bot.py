# web_bypass.py
# FastAPI web service to bypass shortlinks (gplinks, get2.in, etc.)
# Usage: uvicorn web_bypass:app --host 0.0.0.0 --port 8000

import asyncio
import base64
import logging
import re
import time
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin, urlparse, unquote

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, AnyHttpUrl
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web-bypass")

app = FastAPI(title="GPLinks Bypass Service")

# ---- Config ----
DEFAULT_HEADLESS = True
NAV_TIMEOUT = 60_000         # ms
CLICK_TIMEOUT = 12_000       # ms
MAX_TOTAL_WAIT = 60         # seconds per attempt
ATTEMPT_COUNT = 3
TAKE_SCREENSHOT = True      # default include screenshot
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"

# ---- Request / Response models ----
class BypassRequest(BaseModel):
    url: AnyHttpUrl
    headless: Optional[bool] = DEFAULT_HEADLESS
    attempts: Optional[int] = ATTEMPT_COUNT
    include_screenshot: Optional[bool] = True

class BypassResponse(BaseModel):
    final_url: str
    raw_last_url: str
    captcha_detected: bool
    screenshot_b64: Optional[str] = None
    attempts_made: int

# ---- Helpers ----
def looks_final(u: Optional[str]) -> bool:
    if not u:
        return False
    u = u.lower()
    return ("gplinks.co" not in u) and ("gplinks" not in u)

def try_decode_get2_in(url: str) -> Optional[str]:
    """
    Some get2.in links have an encoded target after '?' e.g. get2.in/?<encoded>
    Try to decode that (base64 urlsafe or plain URL-encoded).
    """
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if "get2.in" not in host:
            return None
        query = parsed.query or parsed.path.split("?", 1)[-1]  # fallback
        if not query:
            return None
        # if query is of form k=v, try typical query parsing (but many get2.in use raw value)
        if "=" in query:
            # try to find a value that looks like base64
            parts = query.split("&")
            candidate = None
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

        # try URL-unquote first
        cand = unquote(candidate)
        # try base64 urlsafe decode (with padding)
        try:
            b = cand.encode()
            # add padding if needed
            padding = 4 - (len(b) % 4)
            if padding and padding < 4:
                b += b"=" * padding
            decoded = base64.urlsafe_b64decode(b).decode(errors="ignore")
            # if looks like a URL, return it
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass

        # fallback: if cand itself looks like a url
        if cand.startswith("http"):
            return cand

    except Exception:
        logger.exception("decode get2.in failed")
    return None

async def take_screenshot_base64(page: Page) -> str:
    buf = await page.screenshot(full_page=True)
    b64 = base64.b64encode(buf).decode()
    return b64

# ---- Core bypass logic ----
async def bypass_url_once(page: Page, url: str, attempt_num: int, progress_logger=None):
    """
    Attempts once to follow redirects and clicks. Returns dict with final_url, raw_last_url, captcha_detected, screenshot_b64 (maybe).
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
                            await page.wait_for_timeout(1200)
                        except Exception:
                            pass
                except Exception:
                    pass

            # wait for networkidle
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

            # js redirect sniff
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

            # find external anchors and try navigate
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
                        # if external (not same short domain) consider it final
                        if looks_final(full):
                            try:
                                await page.goto(full, timeout=15000)
                                if looks_final(page.url):
                                    result["final_url"] = page.url
                                    try:
                                        screenshot_b64 = await take_screenshot_base64(page)
                                        result["screenshot_b64"] = screenshot_b64
                                    except Exception:
                                        pass
                                    return result
                            except Exception:
                                # fallback: return the external href
                                result["final_url"] = full
                                return result
                    except Exception:
                        pass
            except Exception:
                pass

            if page.url != last_url:
                last_url = page.url
            await page.wait_for_timeout(1000)

        # timeout reached - best effort
        result["final_url"] = page.url
        try:
            screenshot_b64 = await take_screenshot_base64(page)
            result["screenshot_b64"] = screenshot_b64
        except Exception:
            pass
        return result

    except Exception:
        logger.exception("Error in bypass attempt")
        return result

# ---- HTTP endpoint ----
@app.post("/bypass", response_model=BypassResponse)
async def bypass_endpoint(req: BypassRequest):
    url = str(req.url)
    attempts = max(1, min(10, int(req.attempts or ATTEMPT_COUNT)))
    headless = bool(req.headless)
    include_screenshot = bool(req.include_screenshot)

    # basic safety: only allow http(s)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Unsupported URL scheme")

    logger.info("Received bypass request for %s (attempts=%s headless=%s)", url, attempts, headless)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        final_result = {"final_url": url, "raw_last_url": url, "captcha_detected": False, "screenshot_b64": None}
        attempt_made = 0
        try:
            for i in range(1, attempts + 1):
                attempt_made = i
                # try some human-like tiny actions (non-blocking)
                try:
                    # small mouse move to avoid super-headless
                    await page.mouse.move(100, 100)
                except Exception:
                    pass

                res = await bypass_url_once(page, url, i, progress_logger=lambda t: logger.info("BYPASS: %s", t))
                final_result.update(res)
                # if captcha or final, stop
                if res.get("captcha_detected") or looks_final(res.get("final_url")):
                    break
                # else wait/backoff and try again
                if i < attempts:
                    backoff = (2 ** i) + (0.5 * i)
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
        b64 = final_result.get("screenshot_b64")
        if b64 and not include_screenshot:
            final_result["screenshot_b64"] = None

        return BypassResponse(
            final_url=final_result.get("final_url") or url,
            raw_last_url=final_result.get("raw_last_url") or url,
            captcha_detected=bool(final_result.get("captcha_detected")),
            screenshot_b64=final_result.get("screenshot_b64"),
            attempts_made=attempt_made,
        )CONFIG = {
    "HEADLESS": True,
    "RETRY_ATTEMPTS": 4,
    "SIMULATE_MOUSE": True,
}

# tuning (can be changed in code)
BASE_NAV_TIMEOUT = 60_000
BASE_CLICK_TIMEOUT = 12_000
MAX_TOTAL_WAIT = 60  # seconds per attempt
SCREENSHOT_DIR = "/tmp"
SCREENSHOT_PATH_TEMPLATE = os.path.join(SCREENSHOT_DIR, "gplinks_debug_attempt_{attempt}.png")

# human-like sim
MOUSE_MOVES_PER_ATTEMPT = 12
CLICK_PROBABILITY = 0.6

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gplinks-bot")

# aiogram setup
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# -----------------------------
# Filters / Utilities
# -----------------------------
class AnyTextFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.text)


def extract_url_from_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    # permissive capture of gplinks URL (with or without scheme)
    m = re.search(r'(https?://)?(?:www\.)?gplinks\.co/[^\s)]+', text, re.IGNORECASE)
    if not m:
        return None
    found = m.group(0)
    if not found.lower().startswith("http"):
        found = "https://" + found
    return found


async def send_progress(chat_id: int, text: str, edit_message: Optional[Message] = None, reply_to_message_id: Optional[int] = None) -> Optional[Message]:
    try:
        if edit_message:
            return await edit_message.edit_text(text)
        else:
            return await bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id)
    except Exception:
        logger.exception("Failed to send/edit progress message")
        return None


# -----------------------------
# Playwright helper: small human-like interactions
# -----------------------------
async def do_human_like_actions(page, attempt_num: int):
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        w = viewport.get("width", 1280)
        h = viewport.get("height", 720)

        if random.random() < 0.6:
            y_scroll = random.randint(0, max(0, h // 6))
            try:
                await page.mouse.wheel(0, y_scroll)
                await page.wait_for_timeout(random.randint(250, 700))
            except Exception:
                pass

        moves = MOUSE_MOVES_PER_ATTEMPT + int(attempt_num)
        for _ in range(moves):
            x = random.randint(int(w * 0.1), int(w * 0.9))
            y = random.randint(int(h * 0.1), int(h * 0.9))
            try:
                await page.mouse.move(x, y, steps=random.randint(5, 20))
            except Exception:
                pass
            await page.wait_for_timeout(random.randint(60, 260))

        if random.random() < CLICK_PROBABILITY:
            try:
                cx = w // 2 + random.randint(-150, 150)
                cy = h // 2 + random.randint(-150, 150)
                await page.mouse.click(cx, cy)
                await page.wait_for_timeout(random.randint(120, 700))
            except Exception:
                pass

    except Exception:
        logger.exception("Human-like actions failed (non-fatal)")


# -----------------------------
# Single attempt - improved with explicit button click and screenshots
# -----------------------------
async def attempt_bypass_once(page, url: str, nav_timeout: int, click_timeout: int, attempt: int, progress_callback=None) -> dict:
    result = {"final_url": url, "screenshot": None, "captcha_detected": False, "raw_last_page_url": url}
    screenshot_path = SCREENSHOT_PATH_TEMPLATE.format(attempt=attempt)

    try:
        if progress_callback:
            await progress_callback(f"[Attempt {attempt}] Navigating to {url} ...")

        try:
            await page.goto(url, timeout=nav_timeout)
        except PlaywrightTimeoutError:
            logger.warning("Navigation timed out on attempt %d", attempt)
        except Exception:
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
