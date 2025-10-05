import asyncio
from aiogram import Bot, Dispatcher, types
from playwright.async_api import async_playwright

import os
TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

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

@dp.message_handler()
async def handle_message(message: types.Message):
    if "gplinks.co" not in message.text:
        await message.reply("❌ Please send a valid GPLinks URL.")
        return
    try:
        await message.reply("⏳ Bypassing, please wait...")
        final_url = await bypass_gplinks(message.text.strip())
        await message.reply(f"✅ Final URL:\n{final_url}")
    except Exception as e:
        await message.reply(f"⚠️ Error: {e}")

if __name__ == '__main__':
    from aiogram import executor
    executor.start_polling(dp)