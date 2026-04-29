#!/usr/bin/env python3
"""
web_ui.py — FastAPI web interface for GigE-Cam for pi.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import AsyncGenerator

import zmq
import zmq.asyncio
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials

sys.path.insert(0, os.path.dirname(__file__))
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [web_ui] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

ZMQ_ADDR = "tcp://127.0.0.1:5555"
PREVIEW_PORT = 8765
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
SNAPSHOT_DIR = Path("/data/snapshots")

SESSION_SECRET = secrets.token_bytes(32)
SESSION_COOKIE = "gc_session"
SESSION_MAX_AGE = 86400  # 24 hours

# ---------------------------------------------------------------------------
# Session token helpers
# ---------------------------------------------------------------------------

def _make_session_token() -> str:
    ts = format(int(time.time()), "x")
    sig = hmac.new(SESSION_SECRET, ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _verify_session_token(token: str) -> bool:
    try:
        ts_hex, sig = token.split(".", 1)
        if int(time.time()) - int(ts_hex, 16) > SESSION_MAX_AGE:
            return False
        expected = hmac.new(SESSION_SECRET, ts_hex.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="GigE-Cam", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
security = HTTPBasic(auto_error=False)
zmq_ctx = zmq.asyncio.Context()


# ---------------------------------------------------------------------------
# Middleware: attach session cookie on first successful Basic Auth
# Response dependency injection is broken in Starlette 1.x, so we use
# request.state as a signal and set the cookie here where we control the
# Response object directly.
# ---------------------------------------------------------------------------

@app.middleware("http")
async def session_cookie_middleware(request: Request, call_next):
    request.state.issue_session_cookie = False
    response = await call_next(request)
    if request.state.issue_session_cookie:
        response.set_cookie(
            SESSION_COOKIE, _make_session_token(),
            max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", path="/",
        )
    return response


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _check_auth(credentials: HTTPBasicCredentials) -> bool:
    cfg = config.load()
    auth = cfg.get("auth", {})
    ph = auth.get("password_hash")
    ps = auth.get("password_salt")
    if ph is None or ps is None:
        return (
            secrets.compare_digest(credentials.username, "admin")
            and secrets.compare_digest(credentials.password, "admin")
        )
    return (
        secrets.compare_digest(credentials.username, "admin")
        and config.verify_password(credentials.password, ph, ps)
    )


async def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
):
    # Valid session cookie → fast path, no password check needed
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and _verify_session_token(cookie):
        return

    # Valid Basic Auth → signal middleware to issue a session cookie
    if credentials and _check_auth(credentials):
        request.state.issue_session_cookie = True
        return


    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="GigE-Cam"'},
    )


# ---------------------------------------------------------------------------
# ZMQ helper — new socket per call (REQ is stateless between calls)
# ---------------------------------------------------------------------------

async def zmq_cmd(cmd: dict) -> dict:
    sock = zmq_ctx.socket(zmq.REQ)
    # Linger=0 ensures close() doesn't block if the server is gone
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, 3000)
    sock.setsockopt(zmq.SNDTIMEO, 3000)
    try:
        sock.connect(ZMQ_ADDR)
        await sock.send_json(cmd)
        return await sock.recv_json()
    except Exception as e:
        return {"status": f"error: {e}"}
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=RedirectResponse)
async def root(_=Depends(require_auth)):
    return RedirectResponse("/live")


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_get(request: Request, _=Depends(require_auth)):
    cfg = config.load()
    first = cfg.get("auth", {}).get("first_login", True)
    return templates.TemplateResponse(request, "change_password.html", {
        "first_login": first, "error": None,
    })


@app.post("/change-password", response_class=HTMLResponse)
async def change_password_post(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    _=Depends(require_auth),
):
    cfg = config.load()
    first = cfg.get("auth", {}).get("first_login", True)

    if new_password != confirm_password:
        return templates.TemplateResponse(request, "change_password.html", {
            "first_login": first, "error": "Passwords do not match.",
        })
    if len(new_password) < 6:
        return templates.TemplateResponse(request, "change_password.html", {
            "first_login": first, "error": "Password must be at least 6 characters.",
        })

    h, s = config.hash_password(new_password)
    cfg["auth"].update({"password_hash": h, "password_salt": s, "first_login": False})
    config.save(cfg)
    return RedirectResponse("/live", status_code=303)


@app.get("/live", response_class=HTMLResponse)
async def live(request: Request, _=Depends(require_auth)):
    cfg = config.load()
    if cfg.get("auth", {}).get("first_login", True):
        return RedirectResponse("/change-password", status_code=303)
    return templates.TemplateResponse(request, "live.html", {"active": "live"})


@app.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request, _=Depends(require_auth)):
    cfg = config.load()
    if cfg.get("auth", {}).get("first_login", True):
        return RedirectResponse("/change-password", status_code=303)
    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings", "cfg": cfg, "saved": False, "error": None,
    })


@app.post("/settings", response_class=HTMLResponse)
async def settings_post(request: Request, _=Depends(require_auth)):
    form = await request.form()
    cfg = config.load()
    try:
        res = form.get("resolution", "1640x1232")
        if "x" in res:
            w, h = res.split("x", 1)
            cfg["camera"]["width"] = int(w)
            cfg["camera"]["height"] = int(h)
        cfg["camera"]["framerate"]      = int(form.get("framerate", 30))
        cfg["camera"]["exposure"]       = form.get("exposure", "auto")
        cfg["camera"]["exposure_value"] = int(form.get("exposure_value", 10000))
        cfg["camera"]["gain"]           = float(form.get("gain", 1.0))
        cfg["camera"]["white_balance"]  = form.get("white_balance", "auto")
        cfg["camera"]["rotation"]       = int(form.get("rotation", 0))
        cfg["camera"]["hflip"]          = "hflip" in form
        cfg["camera"]["vflip"]          = "vflip" in form
        cfg["camera"]["night_mode"]     = "night_mode" in form
        cfg["stream"]["bitrate_kbps"]   = int(form.get("bitrate_kbps", 6000))
        cfg["stream"]["codec"]          = form.get("codec", "h264")
        cfg["stream"]["gop"]            = int(form.get("gop", 15))
        cfg["network"]["receiver_ip"]   = form.get("receiver_ip", "192.168.2.5")
        cfg["network"]["receiver_port"] = int(form.get("receiver_port", 5000))
        config.save(cfg)
        await zmq_cmd({"cmd": "apply"})
        return templates.TemplateResponse(request, "settings.html", {
            "active": "settings", "cfg": cfg, "saved": True, "error": None,
        })
    except Exception as e:
        return templates.TemplateResponse(request, "settings.html", {
            "active": "settings", "cfg": cfg, "saved": False, "error": str(e),
        })


@app.get("/system", response_class=HTMLResponse)
async def system_page(request: Request, _=Depends(require_auth)):
    cfg = config.load()
    if cfg.get("auth", {}).get("first_login", True):
        return RedirectResponse("/change-password", status_code=303)
    return templates.TemplateResponse(request, "system.html", {"active": "system"})


# ---------------------------------------------------------------------------
# MJPEG preview stream
# ---------------------------------------------------------------------------

async def _mjpeg_stream() -> AsyncGenerator[bytes, None]:
    """Relay the MJPEG TCP stream from camera_mgr to the browser."""
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", PREVIEW_PORT), timeout=3
        )
        while True:
            chunk = await reader.read(32768)
            if not chunk:
                break
            yield chunk
    except Exception:
        pass
    finally:
        if writer:
            writer.close()


@app.get("/preview")
async def preview(_=Depends(require_auth)):
    return StreamingResponse(
        _mjpeg_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status(_=Depends(require_auth)):
    return JSONResponse(await zmq_cmd({"cmd": "status"}))


@app.get("/api/sysinfo")
async def api_sysinfo(_=Depends(require_auth)):
    return JSONResponse(await zmq_cmd({"cmd": "sysinfo"}))


@app.post("/api/stream/start")
async def api_stream_start(_=Depends(require_auth)):
    return JSONResponse(await zmq_cmd({"cmd": "start"}))


@app.post("/api/stream/stop")
async def api_stream_stop(_=Depends(require_auth)):
    return JSONResponse(await zmq_cmd({"cmd": "stop"}))


@app.post("/api/stream/restart")
async def api_stream_restart(_=Depends(require_auth)):
    return JSONResponse(await zmq_cmd({"cmd": "restart"}))


@app.post("/api/reboot")
async def api_reboot(_=Depends(require_auth)):
    subprocess.Popen(["systemctl", "reboot"])
    return JSONResponse({"status": "rebooting"})


async def _grab_jpeg_from_preview() -> bytes:
    """Extract one complete JPEG frame from the live MJPEG TCP stream."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", PREVIEW_PORT), timeout=3
    )
    try:
        buf = b""
        end_time = time.time() + 5
        while time.time() < end_time:
            chunk = await asyncio.wait_for(reader.read(32768), timeout=2)
            if not chunk:
                break
            buf += chunk
            start = buf.find(b"\xff\xd8")
            if start >= 0:
                end = buf.find(b"\xff\xd9", start + 2)
                if end >= 0:
                    return buf[start: end + 2]
        raise RuntimeError("No JPEG frame found")
    finally:
        writer.close()


@app.get("/api/snapshot")
async def api_snapshot(_=Depends(require_auth)):
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        data = await _grab_jpeg_from_preview()
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        (SNAPSHOT_DIR / f"snap_{ts}.jpg").write_bytes(data)
        return Response(
            content=data, media_type="image/jpeg",
            headers={"Content-Disposition": f"attachment; filename=snap_{ts}.jpg"},
        )
    except Exception as e:
        raise HTTPException(500, f"Snapshot failed: {e}")


@app.get("/api/config/export")
async def api_config_export(_=Depends(require_auth)):
    cfg = config.load()
    export = {k: v for k, v in cfg.items() if k != "auth"}
    return JSONResponse(export, headers={
        "Content-Disposition": "attachment; filename=settings.json",
    })


@app.post("/api/config/import")
async def api_config_import(request: Request, _=Depends(require_auth)):
    body = await request.json()
    cfg = config.load()
    for section in ("camera", "stream", "network"):
        if section in body:
            cfg[section].update(body[section])
    config.save(cfg)
    return JSONResponse({"status": "ok"})


@app.post("/api/config/reset")
async def api_config_reset(_=Depends(require_auth)):
    cfg = config.load()
    fresh = dict(config.DEFAULTS)
    fresh["auth"] = cfg.get("auth", {})
    config.save(fresh)
    return JSONResponse({"status": "ok"})


@app.get("/api/logs")
async def api_logs(_=Depends(require_auth)):
    try:
        result = subprocess.run(
            ["journalctl", "-u", "camstreamer-cam", "-u", "camstreamer-web",
             "-n", "50", "--no-pager", "--output=short"],
            capture_output=True, text=True, timeout=5,
        )
        return JSONResponse({"lines": result.stdout.splitlines()})
    except Exception as e:
        return JSONResponse({"lines": [f"Error reading logs: {e}"]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_ui:app", host="0.0.0.0", port=8080, reload=False)
