import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from playwright.async_api import async_playwright

TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())


async def bypass_gplinks(url):
    async with async_playwright() as p:
        browser = await p.webkit.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)"
        )
        page = await context.new_page()
        await page.goto(url)
        await page.wait_for_timeout(8000)
        return page.url


@dp.message()
async def handle_message(message: Message):
    if "gplinks.co" not in message.text:
        await message.answer("❌ Please send a valid GPLinks URL.")
        return
    try:
        await message.answer("⏳ Bypassing, please wait...")
        final_url = await bypass_gplinks(message.text.strip())
        await message.answer(f"✅ Final URL:\n{final_url}")
    except Exception as e:
        await message.answer(f"⚠️ Error: {e}")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())