# web_bypass.py

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.async_api import async_playwright
import asyncio
import re
from urllib.parse import urlparse

app = FastAPI()

# Mount templates and static (for UI)
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/submit", response_class=HTMLResponse)
async def submit_form(request: Request, url: str = Form(...)):
    final_url = await bypass_gplinks(url)
    return templates.TemplateResponse("index.html", {"request": request, "result_url": final_url})

# Core Bypass Logic
async def bypass_gplinks(input_url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(input_url, timeout=60000)
            await asyncio.sleep(3)

            for i in range(3):
                await page.wait_for_timeout(1000)
                try:
                    verify_btn = await page.wait_for_selector("button.btn.btn-primary", timeout=10000)
                    await verify_btn.click()
                    await page.wait_for_timeout(1500)
                except:
                    pass

            # Wait for redirect to same GPLinks again
            for _ in range(10):
                if "gplinks.co" in page.url:
                    break
                await page.wait_for_timeout(1000)

            # Wait and click on "Get Link"
            await page.wait_for_timeout(5000)
            try:
                get_link_btn = await page.wait_for_selector("a.btn.btn-primary", timeout=15000)
                await get_link_btn.click()
            except:
                pass

            await page.wait_for_load_state("networkidle", timeout=10000)

            final = page.url
            if "gplinks" not in final.lower():
                return final
            else:
                return "Bypass failed. Stuck on GPLinks."

        except Exception as e:
            return f"Error: {e}"
        finally:
            await browser.close()