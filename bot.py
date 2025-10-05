# gplinks_bot_aiogram37_ready.py
"""
GPLinks bypass Telegram bot — aiogram 3.7+ ready and fully responsive.

Features:
- /start (friendly greeting)
- /bypass <url> (or reply to a message containing a gplinks URL)
- Accepts plain messages that include a gplinks URL (no /bypass required)
- /set_headless on|off, /set_retries <n>, /set_mouse on|off, /status
- Retries + human-like interactions + screenshots on CAPTCHA/failure
- Progress messages edited in-chat
Only BOT_TOKEN env var required.
"""

import asyncio
import os
import re
import logging
import time
import random
from typing import Optional
from urllib.parse import urljoin

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import Message, FSInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, BaseFilter
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# -----------------------------
# Minimal required env var
# -----------------------------
TOKEN = os.getenv("BOT_TOKEN")
if TOKEN is None:
    raise SystemExit("BOT_TOKEN environment variable must be set")

# -----------------------------
# Runtime-configurable settings (only BOT_TOKEN required)
# -----------------------------
CONFIG = {
    "HEADLESS": True,        # True => headless; False => visible browser
    "RETRY_ATTEMPTS": 3,     # number of tries
    "SIMULATE_MOUSE": True,  # human-like interactions
}

# Non-runtime tuning constants (can be changed in-code)
BASE_NAV_TIMEOUT = 60_000     # ms
BASE_CLICK_TIMEOUT = 12_000   # ms
MAX_TOTAL_WAIT = 30           # seconds per attempt loop
SCREENSHOT_PATH = "/tmp/gplinks_debug.png"
LOG_TO_TELEGRAM = True        # progress updates are sent to invoking chat
TELEGRAM_LOG_CHAT_ID = None   # if set, logs always go to that chat id

# Retry/backoff settings
BACKOFF_BASE = 2.0     # backoff factor
JITTER_SEC = 1.5       # jitter for backoff

# Human-like interaction settings
MOUSE_MOVES_PER_ATTEMPT = 6
CLICK_PROBABILITY = 0.35

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------
# Aiogram / Bot setup (aiogram 3.7+)
# -----------------------------
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# -----------------------------
# Fallback filter to catch any text message
# -----------------------------
class AnyTextFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.text)


# -----------------------------
# Utilities: send/edit progress messages
# -----------------------------
async def send_progress(chat_id: int, text: str, reply_to_message_id: Optional[int] = None, edit_message: Optional[Message] = None) -> Optional[Message]:
    """
    Send or edit a progress message in Telegram. Returns message object (or None on failure).
    """
    try:
        if edit_message:
            return await edit_message.edit_text(text)
        else:
            return await bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id)
    except Exception:
        logger.exception("Failed to send/edit progress message")
        return None


# -----------------------------
# Human-like interactions utilities
# -----------------------------
async def do_human_like_actions(page, attempt_num: int):
    """
    Small human-like mouse/scroll actions to reduce obvious 'headless' fingerprinting.
    Non-fatal if any action fails.
    """
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        w = viewport.get("width", 1280)
        h = viewport.get("height", 720)

        # small random scroll sometimes
        if random.random() < 0.5:
            y_scroll = random.randint(0, max(0, h // 6))
            try:
                await page.mouse.wheel(0, y_scroll)
                await page.wait_for_timeout(random.randint(250, 700))
            except Exception:
                pass

        # mouse moves
        moves = MOUSE_MOVES_PER_ATTEMPT + int(attempt_num)
        for _ in range(moves):
            x = random.randint(int(w * 0.1), int(w * 0.9))
            y = random.randint(int(h * 0.1), int(h * 0.9))
            try:
                await page.mouse.move(x, y, steps=random.randint(5, 15))
            except Exception:
                pass
            await page.wait_for_timeout(random.randint(60, 220))

        # occasional benign click
        if random.random() < CLICK_PROBABILITY:
            try:
                cx = w // 2 + random.randint(-100, 100)
                cy = h // 2 + random.randint(-100, 100)
                await page.mouse.click(cx, cy)
                await page.wait_for_timeout(random.randint(120, 500))
            except Exception:
                pass

    except Exception:
        logger.exception("Human-like actions failed (non-fatal)")


# -----------------------------
# Low-level single attempt
# -----------------------------
async def attempt_bypass_once(page, url: str, nav_timeout: int, click_timeout: int, progress_callback=None) -> dict:
    """
    Attempt single-pass bypass using selectors/sniffing. Returns dict:
      { final_url, screenshot, captcha_detected, raw_last_page_url }
    """
    result = {"final_url": url, "screenshot": None, "captcha_detected": False, "raw_last_page_url": url}
    try:
        if progress_callback:
            await progress_callback(f"[Attempt] goto {url} (nav_timeout={nav_timeout}ms)...")
        try:
            await page.goto(url, timeout=nav_timeout)
        except PlaywrightTimeoutError:
            logger.warning("Navigation timeout")
        except Exception:
            logger.exception("Navigation error in attempt")

        def looks_final(u: str) -> bool:
            return "gplinks.co" not in (u or "").lower() and "gplinks" not in (u or "").lower()

        start = time.time()
        last_url = page.url

        while time.time() - start < MAX_TOTAL_WAIT:
            current_url = page.url
            result["raw_last_page_url"] = current_url

            if looks_final(current_url) and current_url != url:
                result["final_url"] = current_url
                return result

            try:
                content = await page.content()
            except Exception:
                content = ""

            # basic captcha detection heuristics
            captcha_keywords = ["captcha", "recaptcha", "hcaptcha", "please verify", "i am not a robot"]
            if any(k.lower() in content.lower() for k in captcha_keywords):
                result["captcha_detected"] = True
                try:
                    await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
                    result["screenshot"] = SCREENSHOT_PATH
                except Exception:
                    pass
                return result

            # try clicking common buttons/selectors
            selectors = [
                "a#btn-main", "a[href*='redirect']", "a[href*='http']",
                "a.btn", "button#btn-main", "button", "input[type=submit]"
            ]
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        try:
                            await el.click(timeout=click_timeout)
                            await page.wait_for_timeout(random.randint(900, 1600))
                        except Exception:
                            pass
                except Exception:
                    pass

            # wait for network activity
            try:
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass

            # meta refresh sniff
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

            # external anchor detection & try navigate
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

        # timeout reached: best-effort
        result["final_url"] = page.url
        return result

    except Exception:
        logger.exception("Unexpected error in single attempt")
        return result


# -----------------------------
# Top-level bypass: retries, backoff, simulation
# -----------------------------
async def bypass_gplinks(url: str, progress_callback=None) -> dict:
    """
    Top-level orchestrator that runs multiple attempts and returns the best result.
    """
    attempts = int(CONFIG.get("RETRY_ATTEMPTS", 3))
    headless = bool(CONFIG.get("HEADLESS", True))
    simulate_mouse = bool(CONFIG.get("SIMULATE_MOUSE", True))

    logger.info("Starting bypass: attempts=%d headless=%s simulate_mouse=%s url=%s",
                attempts, headless, simulate_mouse, url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/116.0.0.0 Safari/537.36",
            java_script_enabled=True,
        )
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

                res = await attempt_bypass_once(page, url, nav_timeout, click_timeout, progress_callback=progress_callback)
                last_result = res

                # stop early on captcha or success
                if res.get("captcha_detected"):
                    if progress_callback:
                        await progress_callback("CAPTCHA detected — aborting further attempts.")
                    return res

                def looks_final(u: str) -> bool:
                    return "gplinks.co" not in (u or "").lower() and "gplinks" not in (u or "").lower()

                if looks_final(res.get("final_url")) and res.get("final_url") != url:
                    if progress_callback:
                        await progress_callback(f"Success on attempt {attempt}: {res.get('final_url')}")
                    return res

                # if more attempts remain, backoff + reload
                if attempt < attempts:
                    backoff = (BACKOFF_BASE ** attempt)
                    jitter = random.uniform(-JITTER_SEC, JITTER_SEC)
                    wait_t = max(0.5, backoff + jitter)
                    if progress_callback:
                        await progress_callback(f"No final URL yet — waiting {wait_t:.1f}s before retry {attempt+1}...")
                    await page.wait_for_timeout(int(wait_t * 1000))
                    try:
                        await page.reload(timeout=5000)
                    except Exception:
                        pass

            if progress_callback:
                await progress_callback("All attempts completed — returning best-observed result.")
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
# URL extractor (more permissive)
# -----------------------------
def extract_url_from_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    # capture gplinks variants and shortlinks without protocol too
    m = re.search(r'(https?://)?(?:www\.)?gplinks\.co/[^\s)]+', text, re.IGNORECASE)
    if not m:
        return None
    found = m.group(0)
    # ensure scheme present
    if not found.lower().startswith("http"):
        found = "https://" + found
    return found


# -----------------------------
# Telegram command handlers: runtime toggles & status
# -----------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.reply(
        "👋 Hello! Send me a GPLinks.co URL (or reply to a message containing one) and I'll try to bypass it.\n\n"
        "Commands:\n"
        "/bypass <url> — bypass a link\n"
        "/status — show current settings\n"
        "/set_headless on|off\n"
        "/set_retries <number>\n"
        "/set_mouse on|off\n"
    )


@dp.message(Command("set_headless"))
async def cmd_set_headless(message: Message):
    args = (message.text or "").split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        await message.reply("Usage: /set_headless on|off")
        return
    CONFIG["HEADLESS"] = args[1].lower() == "on"
    await message.reply(f"HEADLESS set to {CONFIG['HEADLESS']}. (No restart required.)")


@dp.message(Command("set_retries"))
async def cmd_set_retries(message: Message):
    args = (message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("Usage: /set_retries <number>")
        return
    CONFIG["RETRY_ATTEMPTS"] = max(1, int(args[1]))
    await message.reply(f"RETRY_ATTEMPTS set to {CONFIG['RETRY_ATTEMPTS']}.")


@dp.message(Command("set_mouse"))
async def cmd_set_mouse(message: Message):
    args = (message.text or "").split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        await message.reply("Usage: /set_mouse on|off")
        return
    CONFIG["SIMULATE_MOUSE"] = args[1].lower() == "on"
    await message.reply(f"SIMULATE_MOUSE set to {CONFIG['SIMULATE_MOUSE']}.")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    status_lines = [f"{k}: {v}" for k, v in CONFIG.items()]
    await message.reply("Current configuration:\n" + "\n".join(status_lines))


# -----------------------------
# Main message handler (catch any text)
# -----------------------------
@dp.message(AnyTextFilter())
async def handle_any_text(message: Message):
    text = (message.text or "").strip()

    # Accept /bypass <url> explicitly too
    if text.lower().startswith("/bypass"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: /bypass <gplinks_url>  — or reply to a message with the command.")
            return
        target = extract_url_from_text(parts[1]) or parts[1].strip()
    else:
        # if replying to a message, prefer the replied message's text
        if message.reply_to_message and message.reply_to_message.text:
            target = extract_url_from_text(message.reply_to_message.text) or extract_url_from_text(text)
        else:
            target = extract_url_from_text(text)

    if not target:
        # no gplinks URL found — optionally respond with help if user asked /start-like texts
        if text.lower().startswith("/start") or text.lower().startswith("hi") or text.lower().startswith("hello"):
            await cmd_start(message)
        # silently ignore other texts to avoid spam
        return

    # start (editable) progress message
    progress_msg: Optional[Message] = None
    if LOG_TO_TELEGRAM:
        progress_msg = await send_progress(chat_id=message.chat.id, text="⏳ Starting bypass (with retries)...", reply_to_message_id=message.message_id)

    async def progress_cb(txt: str):
        nonlocal progress_msg
        logger.info("Progress: %s", txt)
        if LOG_TO_TELEGRAM:
            progress_msg = await send_progress(chat_id=message.chat.id, text=txt, edit_message=progress_msg)

    try:
        if LOG_TO_TELEGRAM:
            await progress_cb(f"Processing: {target}")

        result = await bypass_gplinks(target, progress_callback=progress_cb)

        final = result.get("final_url")
        captcha = result.get("captcha_detected", False)
        screenshot = result.get("screenshot")

        if LOG_TO_TELEGRAM:
            await progress_cb("Bypass finished — preparing results...")

        if captcha:
            await message.reply("⚠️ CAPTCHA detected. I couldn't solve it automatically. See screenshot below for manual review.")
            if screenshot and os.path.exists(screenshot):
                await bot.send_photo(chat_id=message.chat.id, photo=FSInputFile(screenshot), caption="Screenshot (CAPTCHA page)")
            else:
                await message.reply("Could not capture screenshot.")
            await message.reply(f"Partial URL observed: {result.get('raw_last_page_url')}")
            return

        await message.reply(f"✅ Final URL (best-effort):\n{final}")

        if screenshot and os.path.exists(screenshot):
            await bot.send_photo(chat_id=message.chat.id, photo=FSInputFile(screenshot), caption="Debug screenshot")

    except Exception as e:
        logger.exception("Error during bypass flow")
        try:
            if os.path.exists(SCREENSHOT_PATH):
                await bot.send_photo(chat_id=message.chat.id, photo=FSInputFile(SCREENSHOT_PATH), caption="Error screenshot")
        except Exception:
            pass
        await message.reply(f"⚠️ Error while bypassing: {e}")


# -----------------------------
# Entrypoint
# -----------------------------
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())