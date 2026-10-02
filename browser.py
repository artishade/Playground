"""The live browser — one Chromium the user and the agent share.

This is not a screenshot tool. It is a **real, long-lived browser page** that
two actors drive at the same time:

    the user   watches it in the console (a JPEG frame every ~700 ms), clicks,
               types, scrolls and navigates with the mouse and keyboard
    the agent  drives the same page through tools — navigate, click, type,
               extract text, run JS — and every action it takes is visible in
               the frame the user is looking at

Sharing one page is the whole point. An agent that browses in a separate
headless session is a black box; here the user can watch it work, take over
mid-task, or fix the one field the agent got wrong. That is also why frames are
pushed over the *same* SSE connection as shell output — one stream, one clock.

    GET    /agent/browser                 state: status, url, title, viewport
    POST   /agent/browser/start           launch (idempotent)
    POST   /agent/browser/stop            close the browser
    POST   /agent/browser/navigate        {url}
    POST   /agent/browser/action          {action: click|type|scroll|back|…}
    POST   /agent/browser/frame           one JPEG, as image/jpeg
    GET    /agent/browser/stream          SSE: a frame whenever the page changes

Playwright is an **optional** dependency on purpose. The terminal's promise is
that `terminal/` runs anywhere with three Python packages; a browser is 400 MB
of Chromium that many hosts cannot carry. So it degrades honestly: with no
Playwright installed, every route answers `code: browser_unavailable` and says
exactly how to install it — the rest of the terminal is untouched.

    pip install playwright && python3 -m playwright install --with-deps chromium
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from typing import Any

log = logging.getLogger("terminal.browser")

# A page is a stateful, expensive thing; two is already generous for one host.
MAX_PAGES = 1
FRAME_QUALITY = 62          # JPEG quality — the frame is a UI, not an archive
FRAME_MAX_WIDTH = 1280      # downscale wider viewports before encoding
IDLE_CLOSE_S = 30 * 60      # a forgotten browser is a 400 MB leak
DEFAULT_TIMEOUT_MS = 30000
NAV_TIMEOUT_MS = 45000

URL_RE = re.compile(r"^(https?|about|data|file):", re.IGNORECASE)


class BrowserError(RuntimeError):
    code = "browser_error"


class BrowserUnavailable(BrowserError):
    """Playwright (or its Chromium) is not installed on this host."""

    code = "browser_unavailable"


INSTALL_HINT = (
    "The live browser needs Playwright and Chromium, which are not part of the "
    "terminal's three dependencies: run `pip install playwright` and "
    "`python3 -m playwright install --with-deps chromium` on this host."
)


def normalise_url(raw: str) -> str:
    """`example.com` is what people type; a browser needs a scheme."""
    url = (raw or "").strip()
    if not url:
        raise ValueError("a url is required")
    if not URL_RE.match(url):
        url = "https://" + url.lstrip("/")
    if not URL_RE.match(url):
        raise ValueError(f"'{raw}' is not a usable url")
    return url


def _require_playwright() -> Any:
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
    except ImportError as err:
        raise BrowserUnavailable(INSTALL_HINT) from err
    return async_playwright


# --------------------------------------------------------------------------- #
# The session — one browser, one page, shared
# --------------------------------------------------------------------------- #


class BrowserSession:
    """Owns the Playwright objects and serialises every action against them.

    Playwright is not thread-safe and a page is not concurrency-safe either, so
    a single lock guards the whole surface: the user's click and the agent's
    type can never interleave halfway. Frames are the exception — they read the
    page and are dropped rather than queued if another action holds the lock.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.started_at = 0.0
        self.last_used = 0.0
        self.last_error = ""
        self.frames_sent = 0
        self.actions = 0
        self.viewport = {"width": 1280, "height": 800}
        self._last_signature = ""
        self._subscribers: list[asyncio.Queue] = []

    # ---- lifecycle ------------------------------------------------------- #

    @property
    def alive(self) -> bool:
        return bool(self.browser and self.browser.is_connected() and self.page)

    async def start(self, width: int = 1280, height: int = 800) -> dict[str, Any]:
        """Launch Chromium. Idempotent: a live browser is returned as-is."""
        if self.alive:
            self.last_used = time.time()
            return await self.state()

        async_playwright = _require_playwright()
        async with self.lock:
            if self.alive:
                return await self.state()
            self.viewport = {
                "width": max(480, min(2560, int(width or 1280))),
                "height": max(360, min(1600, int(height or 800))),
            }
            try:
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    # --no-sandbox is required in a container without user
                    # namespaces; this process is already root, so the sandbox
                    # would add nothing here and its absence would break launch.
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--hide-scrollbars",
                        "--mute-audio",
                    ],
                )
                self.context = await self.browser.new_context(
                    viewport=self.viewport,
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 NovaRouter/2.2"
                    ),
                    ignore_https_errors=True,
                )
                self.page = await self.context.new_page()
                self.page.set_default_timeout(DEFAULT_TIMEOUT_MS)
                self.page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                self.page.on("close", lambda _p: self._publish(b"", "closed"))
                await self.page.goto("about:blank")
            except BrowserUnavailable:
                raise
            except Exception as err:
                await self._teardown()
                self.last_error = f"{err.__class__.__name__}: {err}"
                raise BrowserError(
                    "Chromium failed to launch "
                    f"({err.__class__.__name__}). If the binary is missing run "
                    "`python3 -m playwright install --with-deps chromium`."
                ) from err
            self.started_at = self.last_used = time.time()
            self.last_error = ""
            log.info("browser: chromium up at %sx%s", self.viewport["width"], self.viewport["height"])
            return await self.state()

    async def stop(self) -> dict[str, Any]:
        async with self.lock:
            await self._teardown()
        return {"ok": True, "running": False}

    async def _teardown(self) -> None:
        for closer in (self.context, self.browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:                     # noqa: BLE001 — teardown is best-effort
                pass
        try:
            if self.playwright is not None:
                await self.playwright.stop()
        except Exception:                          # noqa: BLE001
            pass
        self.playwright = self.browser = self.context = self.page = None
        self.started_at = 0.0
        self._last_signature = ""

    async def reap_if_idle(self) -> bool:
        """Close a browser nobody has touched. Called from the health poll."""
        if self.alive and self.last_used and (time.time() - self.last_used) > IDLE_CLOSE_S:
            log.info("browser: closing after %ss idle", IDLE_CLOSE_S)
            await self.stop()
            return True
        return False

    # ---- state ----------------------------------------------------------- #

    async def state(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "running": self.alive,
            "viewport": self.viewport,
            "uptime_s": int(time.time() - self.started_at) if self.started_at else 0,
            "idle_s": int(time.time() - self.last_used) if self.last_used else 0,
            "frames_sent": self.frames_sent,
            "actions": self.actions,
            "url": "",
            "title": "",
            "can_go_back": False,
            "can_go_forward": False,
            "last_error": self.last_error or None,
        }
        if not self.alive:
            return info
        try:
            info["url"] = self.page.url or ""
            info["title"] = (await self.page.title()) or ""
            history = await self.page.evaluate(
                "() => ({back: history.length > 1, forward: false})"
            )
            info["can_go_back"] = bool(history.get("back"))
        except Exception as err:                   # a navigating page can throw here
            info["last_error"] = f"{err.__class__.__name__}: {err}"
        return info

    # ---- frames ---------------------------------------------------------- #

    async def frame(self, quality: int = FRAME_QUALITY) -> bytes:
        """One JPEG of the current viewport."""
        if not self.alive:
            raise BrowserError("the browser is not running")
        if self.lock.locked():
            # An action is mid-flight; serving a stale frame beats blocking the
            # frame loop behind a slow navigation.
            return b""
        async with self.lock:
            self.last_used = time.time()
            try:
                raw = await self.page.screenshot(
                    type="jpeg", quality=max(20, min(90, int(quality or FRAME_QUALITY))),
                    caret="initial", animations="disabled",
                )
            except Exception as err:
                self.last_error = f"{err.__class__.__name__}: {err}"
                raise BrowserError(f"cannot capture a frame: {err}") from err
            self.frames_sent += 1
            return raw

    async def signature(self) -> str:
        """A cheap hash of what is on screen, so the loop can skip idle frames."""
        if not self.alive:
            return ""
        try:
            value = await self.page.evaluate(
                "() => [location.href, document.title, "
                "document.body ? document.body.innerText.length : 0, "
                "document.body ? document.body.scrollTop : 0].join('|')"
            )
            return str(value)
        except Exception:
            return ""

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def _publish(self, data: bytes, event: str = "frame") -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait({"event": event, "data": data})
            except asyncio.QueueFull:
                pass                               # a slow client skips a frame, never lags the page

    # ---- actions --------------------------------------------------------- #

    async def navigate(self, url: str, wait: str = "domcontentloaded") -> dict[str, Any]:
        target = normalise_url(url)
        if not self.alive:
            await self.start()
        async with self.lock:
            self.last_used = time.time()
            self.actions += 1
            try:
                response = await self.page.goto(target, wait_until=wait)
            except Exception as err:
                self.last_error = f"{err.__class__.__name__}: {err}"
                raise BrowserError(
                    f"could not load {target} ({err.__class__.__name__}). "
                    "The site may be slow, blocking automation, or unreachable."
                ) from err
            status = response.status if response is not None else None
        state = await self.state()
        state["status_code"] = status
        state["requested"] = target
        return state

    async def click(self, selector: str = "", x: int | None = None,
                    y: int | None = None, text: str = "") -> dict[str, Any]:
        """Click by selector, by visible text, or at a viewport coordinate."""
        async with self.lock:
            self._assert_alive()
            self.last_used = time.time()
            self.actions += 1
            target = (selector or "").strip()
            try:
                if x is not None and y is not None:
                    await self.page.mouse.click(float(x), float(y))
                    how = f"({int(x)},{int(y)})"
                elif target:
                    locator = self.page.locator(target).first
                    await locator.click(timeout=DEFAULT_TIMEOUT_MS)
                    how = target
                elif text:
                    locator = self.page.get_by_text(text, exact=False).first
                    await locator.click(timeout=DEFAULT_TIMEOUT_MS)
                    how = f"text={text!r}"
                else:
                    raise BrowserError("a click needs a selector, text, or x/y coordinates")
            except BrowserError:
                raise
            except Exception as err:
                raise BrowserError(
                    f"could not click {target or text or f'({x},{y})'}: "
                    f"{err.__class__.__name__}. Use the frame to find the real element, "
                    "or click by coordinates."
                ) from err
        await self._settle()
        state = await self.state()
        state["clicked"] = how
        return state

    async def type_text(self, text: str, selector: str = "", submit: bool = False,
                        clear: bool = True) -> dict[str, Any]:
        if not isinstance(text, str):
            raise BrowserError("text must be a string")
        async with self.lock:
            self._assert_alive()
            self.last_used = time.time()
            self.actions += 1
            try:
                if selector:
                    locator = self.page.locator(selector).first
                    if clear:
                        await locator.fill("")
                    await locator.type(text, delay=15)
                else:
                    # No selector: type into whatever has focus — how a user types.
                    if clear:
                        await self.page.keyboard.press("Control+A")
                        await self.page.keyboard.press("Backspace")
                    await self.page.keyboard.type(text, delay=15)
                if submit:
                    await self.page.keyboard.press("Enter")
            except Exception as err:
                raise BrowserError(f"could not type: {err.__class__.__name__}: {err}") from err
        await self._settle()
        return await self.state()

    async def press(self, key: str) -> dict[str, Any]:
        async with self.lock:
            self._assert_alive()
            self.last_used = time.time()
            self.actions += 1
            try:
                await self.page.keyboard.press(key or "Enter")
            except Exception as err:
                raise BrowserError(f"could not press {key!r}: {err}") from err
        await self._settle()
        return await self.state()

    async def scroll(self, direction: str = "down", amount: int = 600) -> dict[str, Any]:
        delta = abs(int(amount or 600))
        if (direction or "down").lower() in ("up", "top"):
            delta = -delta
        async with self.lock:
            self._assert_alive()
            self.last_used = time.time()
            self.actions += 1
            try:
                if (direction or "").lower() == "bottom":
                    await self.page.keyboard.press("End")
                else:
                    await self.page.mouse.wheel(0, delta)
            except Exception as err:
                raise BrowserError(f"could not scroll: {err}") from err
        await self._settle(0.35)
        return await self.state()

    async def go_back(self) -> dict[str, Any]:
        async with self.lock:
            self._assert_alive()
            self.last_used = time.time()
            self.actions += 1
            try:
                await self.page.go_back(wait_until="domcontentloaded")
            except Exception as err:
                raise BrowserError(f"could not go back: {err}") from err
        return await self.state()

    async def resize(self, width: int, height: int) -> dict[str, Any]:
        async with self.lock:
            self._assert_alive()
            self.last_used = time.time()
            self.viewport = {
                "width": max(480, min(2560, int(width or 1280))),
                "height": max(360, min(1600, int(height or 800))),
            }
            try:
                await self.page.set_viewport_size(self.viewport)
            except Exception as err:
                raise BrowserError(f"could not resize: {err}") from err
        return await self.state()

    async def _settle(self, seconds: float = 0.6) -> None:
        """Give a click/type a moment to land before the next frame is read."""
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:
            pass
        await asyncio.sleep(seconds)

    def _assert_alive(self) -> None:
        if not self.alive:
            raise BrowserError("the browser is not running — start it first")


SESSION = BrowserSession()
