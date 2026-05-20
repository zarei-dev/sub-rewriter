"""
Subscription rewriter — Persian RTL panel + v2ray-family auto-import.

Behavior:
  - v2ray-family clients (UA-detected) → base64 list, auto-imports nodes
  - Browsers → Persian RTL HTML "user panel" (no protocol/client references)
  - ?raw=1 forces base64; ?html=1 forces HTML (for testing)

Hides 'vless://' from visible HTML by base64-encoding each URL server-side;
the page's JS decodes only when user clicks copy or QR is rendered.
"""

import base64
import os
import time
from urllib.parse import (
    urlparse, parse_qsl, urlencode, urlunparse, unquote, quote,
)

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


# ---------- Config (override via environment) ----------

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://sub.pokify.online").rstrip("/")
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "10"))

PARAM_OVERRIDES = {
    "security": "tls",
    "fp": "chrome",
    "alpn": "http/1.1",
    "encryption": "none",
}

PARAM_DEFAULTS = {}

PATH_REWRITES = {
    "/ebe/": "/api/v2/messages/stream",
    "/test/": "/socket.io/",
}

REMARK_PREFIX = os.environ.get("REMARK_PREFIX", "")

PANEL_NAME = os.environ.get("PANEL_NAME", "migrado")
PANEL_TAGLINE = os.environ.get("PANEL_TAGLINE", "پنل کاربری")
PUBLIC_SUB_URL_BASE = os.environ.get("PUBLIC_SUB_URL_BASE", "").rstrip("/")


# ---------- App ----------

app = FastAPI(title="panel", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")
app.mount("/sub/_static", StaticFiles(directory="static"), name="static")


# ---------- UA detection ----------

CLIENT_UA_HINTS = (
    "v2ray", "v2rayn", "v2rayng", "xray", "nekobox", "nekoray", "shadowrocket",
    "clash", "stash", "loon", "surge", "happ", "streisand", "fairvpn", "v2box",
    "hiddify", "sing-box", "sagernet", "matsuri", "v2flyclient", "karing",
    "foxray", "kitsunebi", "throne", "qv2ray", "leaf", "openpanel",
    "v2rayu", "shadowsocks", "shadowsocksx", "outline",
)
BROWSER_UA_HINTS = ("mozilla", "chrome", "safari", "firefox", "edge", "opera", "webkit")


def is_browser(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    if any(h in ua for h in CLIENT_UA_HINTS):
        return False
    if any(h in ua for h in BROWSER_UA_HINTS):
        return True
    return False


# ---------- Helpers ----------

def b64_pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def try_decode_sub(body: str) -> str:
    body = body.strip()
    if "vless://" in body or "vmess://" in body or "trojan://" in body:
        return body
    try:
        return base64.b64decode(b64_pad(body)).decode("utf-8", errors="replace")
    except Exception:
        return body


def rewrite_vless(url: str) -> str:
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))

    for k, v in PARAM_OVERRIDES.items():
        params[k] = v
    for k, v in PARAM_DEFAULTS.items():
        params.setdefault(k, v)

    if "path" in params:
        decoded_path = unquote(params["path"])
        for src, dst in PATH_REWRITES.items():
            if decoded_path == src or decoded_path.startswith(src):
                params["path"] = decoded_path.replace(src, dst, 1)
                break

    new_query = urlencode(params, safe="/:")
    fragment = parsed.fragment
    if REMARK_PREFIX and fragment:
        decoded_frag = unquote(fragment)
        if not decoded_frag.startswith(REMARK_PREFIX):
            fragment = quote(f"{REMARK_PREFIX}{decoded_frag}", safe="")

    return urlunparse(parsed._replace(query=new_query, fragment=fragment))


def rewrite_body(decoded: str) -> list[str]:
    out = []
    for line in decoded.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("vless://"):
            out.append(rewrite_vless(line))
        else:
            out.append(line)
    return out


def parse_userinfo(header_value: str) -> dict:
    info = {}
    if not header_value:
        return info
    for chunk in header_value.split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            try:
                info[k.strip()] = int(v.strip())
            except ValueError:
                info[k.strip()] = v.strip()
    return info


def parse_link_for_display(url: str) -> dict:
    """
    Display-only fields. The full URL is base64-encoded into `payload` so
    'vless://...' never appears in the HTML source. JS decodes on demand.
    """
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    payload_b64 = base64.b64encode(url.encode()).decode()
    return {
        "remark": unquote(parsed.fragment) or "—",
        "host": parsed.hostname or "",
        "port": parsed.port or "",
        "type": params.get("type", ""),
        "security": params.get("security", ""),
        "payload": payload_b64,
    }


# ---------- Routes ----------

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/sub/{token}")
async def sub(token: str, request: Request, raw: int = 0, html: int = 0):
    upstream_url = f"{UPSTREAM_BASE}/{token}"
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, verify=True) as client:
            r = await client.get(
                upstream_url,
                headers={
                    "User-Agent": request.headers.get("user-agent", "v2rayN"),
                    "Accept": "*/*",
                },
                follow_redirects=True,
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail="upstream non-200")

    decoded = try_decode_sub(r.text)
    links = rewrite_body(decoded)
    new_body = "\n".join(links) + "\n"

    passthrough = {}
    for h in ("subscription-userinfo", "profile-update-interval", "profile-title",
              "profile-web-page-url", "support-url"):
        if h in r.headers:
            passthrough[h] = r.headers[h]

    ua = request.headers.get("user-agent", "")
    serve_html = html == 1 or (raw != 1 and is_browser(ua))

    if serve_html:
        userinfo = parse_userinfo(passthrough.get("subscription-userinfo", ""))
        nodes = [parse_link_for_display(l) for l in links if l.startswith("vless://")]
        return templates.TemplateResponse(
            "sub.html",
            {
                "request": request,
                "panel_name": PANEL_NAME,
                "tagline": PANEL_TAGLINE,
                "nodes": nodes,
                "sub_url": (PUBLIC_SUB_URL_BASE + "/" + token) if PUBLIC_SUB_URL_BASE else str(request.url).split("?")[0],
                "userinfo": userinfo,
                "now": int(time.time()),
            },
        )

    encoded = base64.b64encode(new_body.encode()).decode()
    headers = dict(passthrough)
    headers["content-type"] = "text/plain; charset=utf-8"
    headers["cache-control"] = "no-store"
    if "profile-title" not in headers:
        headers["profile-title"] = base64.b64encode(PANEL_NAME.encode()).decode()
    if "profile-update-interval" not in headers:
        headers["profile-update-interval"] = "12"
    return Response(content=encoded, headers=headers)


@app.get("/")
async def root():
    return PlainTextResponse("ok")
