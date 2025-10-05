# web_bypass.py
"""
GPLinks bypass service using cloudscraper + BeautifulSoup (synchronous worker wrapped with asyncio).
Single-file FastAPI app with embedded UI (no templates).
"""
import asyncio
import time
import re
import logging
from typing import Optional
from urllib.parse import urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, AnyHttpUrl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gplinks-cloudscraper")

app = FastAPI(title="GPLinks Bypass (cloudscraper)")

# ---------- Config ----------
DEFAULT_WAIT_SECONDS = 15
DEFAULT_ATTEMPTS = 3
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"

# ---------- Models ----------
class BypassRequest(BaseModel):
    url: AnyHttpUrl
    attempts: Optional[int] = DEFAULT_ATTEMPTS
    wait_seconds: Optional[int] = DEFAULT_WAIT_SECONDS

class BypassResponse(BaseModel):
    final_url: str
    raw_response_url: str
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