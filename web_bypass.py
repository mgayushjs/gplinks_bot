from fastapi import FastAPI, Form, Request, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, AnyHttpUrl
from typing import Optional
import os
import base64
from playwright.async_api import async_playwright, TimeoutError

app = FastAPI(title="GPLinks Web Bypasser")

API_KEY = os.getenv("API_KEY")


class BypassRequest(BaseModel):
    url: AnyHttpUrl
    headless: Optional[bool] = True
    attempts: Optional[int] = 3
    include_screenshot: Optional[bool] = False


class BypassResponse(BaseModel):
    final_url: str
    screenshot_b64: Optional[str]
    attempts_made: int


@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <html>
        <head>
            <title>GPLinks Bypasser</title>
        </head>
        <body style="font-family: sans-serif; margin: 40px;">
            <h2>GPLinks Bypasser 🔗</h2>
            <form method="post" action="/submit">
                <input name="url" type="text" placeholder="Paste GPLinks URL here" style="width: 400px; padding: 8px;" required/>
                <br><br>
                <button type="submit" style="padding: 8px 16px;">Bypass Link</button>
            </form>
        </body>
    </html>
    """


@app.post("/submit", response_class=HTMLResponse)
async def handle_form(url: str = Form(...)):
    try:
        result = await bypass_link(url)
        return f"""
        <html>
            <head><title>Bypassed</title></head>
            <body style="font-family: sans-serif; margin: 40px;">
                <h2>✅ Final URL</h2>
                <a href="{result['final_url']}" target="_blank">{result['final_url']}</a><br><br>
                <strong>Attempts:</strong> {result['attempts_made']}<br>
                {'<br><img src="data:image/png;base64,' + result['screenshot_b64'] + '" width="600"/>' if result['screenshot_b64'] else ''}
                <br><br><a href="/">🔙 Back</a>
            </body>
        </html>
        """
    except Exception as e:
        return f"<html><body><h3>Error</h3><pre>{str(e)}</pre><br><a href='/'>🔙 Try Again</a></body></html>"


@app.post("/bypass", response_model=BypassResponse)
async def bypass_api(req: BypassRequest, x_api_key: Optional[str] = Header(None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return await bypass_link(req.url, req.headless, req.attempts, req.include_screenshot)


# --- core logic ---
async def bypass_link(url: str, headless=True, attempts=3, include_screenshot=False):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()

        final_url = url
        attempt = 0
        screenshot = None

        try:
            for attempt in range(1, attempts + 1):
                await page.goto(url, timeout=60000)
                await page.wait_for_timeout(2000)

                # Click through 3 verification steps
                for _ in range(3):
                    try:
                        btn = await page.wait_for_selector("a#linkbtn, button, a.btn", timeout=15000)
                        if btn:
                            await btn.click()
                            await page.wait_for_timeout(5000)
                    except TimeoutError:
                        break
                    except Exception:
                        continue

                # Final "Get Link" click
                try:
                    getlink = await page.wait_for_selector("a#linkbtn, button", timeout=10000)
                    if getlink:
                        await getlink.click()
                        await page.wait_for_timeout(4000)
                except Exception:
                    pass

                # After redirection
                current_url = page.url
                if "gplinks" not in current_url.lower():
                    final_url = current_url
                    break

        finally:
            if include_screenshot:
                try:
                    ss = await page.screenshot(full_page=True)
                    screenshot = base64.b64encode(ss).decode()
                except Exception:
                    screenshot = None
            await context.close()
            await browser.close()

        return {
            "final_url": final_url,
            "screenshot_b64": screenshot,
            "attempts_made": attempt
        }