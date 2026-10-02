"""The browser's HTTP surface — the frame stream and the user's controls.

Two clients talk to this:

    the console   GET /stream (frames as base64 SSE events) plus POST /action,
                  /navigate, /start, /stop — the user's mouse and keyboard
    the agent     the same routes, through its tools (see agentbox.py), so an
                  agent action and a user action are the *same* call and the
                  page can never tell them apart

Frames travel as SSE `data:` events carrying base64 JPEG, because a browser
`<img>` cannot carry an auth header and an EventSource can only carry a cookie.
The token therefore rides in the query string for this one route, which is the
usual, deliberate trade for a live view — it is a read-only stream of a page the
holder of the token can already drive.

A frame is only sent when the page actually changed (url, title, text length,
scroll), so an idle page costs nothing. `?force=1` sends regardless.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .browser import BrowserError, BrowserUnavailable, SESSION, normalise_url

log = logging.getLogger("terminal.browser_api")

router = APIRouter()

FRAME_INTERVAL = 0.7        # seconds between candidate frames
FRAME_QUALITY = 62
KEEPALIVE_S = 15.0          # comment frames, so a proxy does not drop the stream


def fail(err: Exception) -> JSONResponse:
    if isinstance(err, BrowserUnavailable):
        return JSONResponse({"error": str(err), "code": err.code,
                             "install": "pip install playwright && "
                                        "python3 -m playwright install --with-deps chromium"},
                            status_code=503)
    if isinstance(err, BrowserError):
        return JSONResponse({"error": str(err), "code": err.code}, status_code=409)
    if isinstance(err, ValueError):
        return JSONResponse({"error": str(err), "code": "bad_request"}, status_code=400)
    log.exception("browser route failed")
    return JSONResponse({"error": f"{err.__class__.__name__}: {err}",
                         "code": "browser_error"}, status_code=500)


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------------- #
# State and lifecycle
# --------------------------------------------------------------------------- #


@router.get("")
async def state():
    """Everything the console needs to draw its toolbar."""
    try:
        info = await SESSION.state()
    except Exception as err:                      # noqa: BLE001
        return fail(err)
    info["available"] = _available()
    return {"ok": True, "browser": info}


def _available() -> bool:
    """Is Playwright importable? Cheap enough to answer per request."""
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except Exception:                             # noqa: BLE001
        return False


@router.post("/start")
async def start(request: Request):
    body = await _body(request)
    try:
        info = await SESSION.start(_int(body.get("width"), 1280), _int(body.get("height"), 800))
    except Exception as err:                      # noqa: BLE001
        return fail(err)
    return {"ok": True, "browser": info}


@router.post("/stop")
async def stop():
    try:
        await SESSION.stop()
    except Exception as err:                      # noqa: BLE001
        return fail(err)
    return {"ok": True, "browser": {"running": False}}


@router.post("/resize")
async def resize(request: Request):
    body = await _body(request)
    try:
        info = await SESSION.resize(_int(body.get("width"), 1280), _int(body.get("height"), 800))
    except Exception as err:                      # noqa: BLE001
        return fail(err)
    return {"ok": True, "browser": info}


# --------------------------------------------------------------------------- #
# Driving it — the same routes for the user and the agent
# --------------------------------------------------------------------------- #


@router.post("/navigate")
async def navigate(request: Request):
    body = await _body(request)
    raw = body.get("url")
    if not isinstance(raw, str) or not raw.strip():
        return JSONResponse({"error": "url is required", "code": "bad_request"}, status_code=400)
    wait = body.get("wait") if body.get("wait") in ("load", "domcontentloaded", "networkidle") else "domcontentloaded"
    try:
        info = await SESSION.navigate(raw, wait)
    except Exception as err:                      # noqa: BLE001
        return fail(err)
    return {"ok": True, "browser": info}


@router.post("/action")
async def action(request: Request):
    """One user/agent action: click, type, press, scroll, back, goto."""
    body = await _body(request)
    kind = str(body.get("action") or "").strip().lower()
    try:
        if kind == "click":
            x, y = body.get("x"), body.get("y")
            info = await SESSION.click(
                str(body.get("selector") or ""),
                _int(x, 0) if x is not None else None,
                _int(y, 0) if y is not None else None,
                str(body.get("text") or ""),
            )
        elif kind == "type":
            text = body.get("text")
            if not isinstance(text, str):
                return JSONResponse({"error": "text is required", "code": "bad_request"},
                                    status_code=400)
            info = await SESSION.type_text(
                text,
                str(body.get("selector") or ""),
                bool(body.get("submit")),
                bool(body.get("clear", True)),
            )
        elif kind == "press":
            info = await SESSION.press(str(body.get("key") or "Enter"))
        elif kind == "scroll":
            info = await SESSION.scroll(str(body.get("direction") or "down"),
                                        _int(body.get("amount"), 600))
        elif kind == "back":
            info = await SESSION.go_back()
        elif kind == "goto":
            info = await SESSION.navigate(str(body.get("url") or ""))
        else:
            return JSONResponse(
                {"error": f"unknown action '{kind}' — use click, type, press, scroll, back or goto",
                 "code": "bad_request"},
                status_code=400,
            )
    except Exception as err:                      # noqa: BLE001
        return fail(err)
    return {"ok": True, "browser": info}


# --------------------------------------------------------------------------- #
# One frame, on demand
# --------------------------------------------------------------------------- #


@router.get("/frame")
async def frame(quality: int = FRAME_QUALITY):
    """A single JPEG. Handy for a <img> refresh and for the agent's own eyes."""
    try:
        raw = await SESSION.frame(quality)
    except Exception as err:                      # noqa: BLE001
        return fail(err)
    if not raw:
        return Response(status_code=204)          # busy; the client keeps the last frame
    return Response(content=raw, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "Content-Length": str(len(raw))})


# --------------------------------------------------------------------------- #
# The live stream
# --------------------------------------------------------------------------- #


@router.get("/stream")
async def stream(request: Request, force: int = 0, quality: int = FRAME_QUALITY):
    """SSE: a base64 JPEG whenever the page changed, plus small status events."""
    queue = SESSION.subscribe()

    async def gen():
        last_sig = None
        last_frame = 0.0
        last_beat = time.time()
        try:
            yield _sse("hello", {"interval_ms": int(FRAME_INTERVAL * 1000),
                                 "quality": quality})
            while True:
                if await request.is_disconnected():
                    break
                now = time.time()

                # 1. A frame, if the page moved (or the client asked for one).
                if now - last_frame >= FRAME_INTERVAL:
                    last_frame = now
                    if SESSION.alive:
                        signature = await SESSION.signature()
                        if force or signature != last_sig:
                            last_sig = signature
                            try:
                                raw = await SESSION.frame(quality)
                            except BrowserError as err:
                                yield _sse("error", {"error": str(err)})
                                raw = b""
                            if raw:
                                yield _sse("frame", {
                                    "jpeg": base64.b64encode(raw).decode("ascii"),
                                    "bytes": len(raw),
                                })
                        # A status tick every few frames keeps the URL/title live
                        # without paying for a screenshot.
                        if now - last_beat >= 4.0:
                            last_beat = now
                            yield _sse("state", await _short_state())

                # 2. Anything a publisher pushed (page closed, navigation done).
                try:
                    pushed = queue.get_nowait()
                    yield _sse(pushed["event"], {"closed": pushed["event"] == "closed"})
                except asyncio.QueueEmpty:
                    pass

                # 3. A comment keeps intermediaries from closing an idle stream.
                if now - last_beat >= KEEPALIVE_S:
                    last_beat = now
                    yield ": keepalive\n\n"

                await asyncio.sleep(0.2)
        finally:
            SESSION.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


async def _short_state() -> dict[str, Any]:
    info = await SESSION.state()
    return {
        "running": info["running"], "url": info["url"], "title": info["title"],
        "can_go_back": info["can_go_back"], "viewport": info["viewport"],
        "actions": info["actions"],
    }


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"