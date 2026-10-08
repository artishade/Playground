#!/usr/bin/env python3
"""
Genymotion Emulator Control Server

This server acts as a bridge between the AI agent MCP server and the Genymotion
Device Web Player. It maintains a WebSocket connection to the Genymotion WebRTC
signaling server and provides an HTTP/WebSocket API for controlling the emulator.

The Genymotion web player uses a WebSocket connection for:
1. SDP signaling (WebRTC connection setup)
2. ICE candidates exchange
3. Data channel establishment for input events

Input events are sent as JSON messages:
  {"type": "MOUSE_PRESS", "x": 100, "y": 100}
  {"type": "MOUSE_RELEASE", "x": 100, "y": 100}
  {"type": "MOUSE_MOVE", "x": 150, "y": 150}
  {"type": "KEYBOARD_PRESS", "keycode": 13}
  {"type": "KEYBOARD_RELEASE", "keychar": "a", "keycode": 97}

Note: x/y coordinates in MOUSE events are in device screen pixels.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logger = logging.getLogger("geny-control")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


class KeyCode(Enum):
    """Android key codes (from Android.view.KeyEvent)."""
    KEYCODE_UNKNOWN = 0
    KEYCODE_SOFT_LEFT = 1
    KEYCODE_SOFT_RIGHT = 2
    KEYCODE_HOME = 3
    KEYCODE_BACK = 4
    KEYCODE_APP_SWITCH = 5
    KEYCODE_ENDCALL = 6
    KEYCODE_0 = 7
    KEYCODE_1 = 8
    KEYCODE_2 = 9
    KEYCODE_3 = 10
    KEYCODE_4 = 11
    KEYCODE_5 = 12
    KEYCODE_6 = 13
    KEYCODE_7 = 14
    KEYCODE_8 = 15
    KEYCODE_9 = 16
    KEYCODE_STAR = 17
    KEYCODE_HASH = 18
    KEYCODE_DPAD_UP = 19
    KEYCODE_DPAD_DOWN = 20
    KEYCODE_DPAD_LEFT = 21
    KEYCODE_DPAD_RIGHT = 22
    KEYCODE_DPAD_CENTER = 23
    KEYCODE_VOLUME_UP = 24
    KEYCODE_VOLUME_DOWN = 25
    KEYCODE_VOLUME_MUTE = 26
    KEYCODE_POWER = 26
    KEYCODE_CAMERA = 27
    KEYCODE_ENTER = 66
    KEYCODE_ESCAPE = 111
    KEYCODE_DEL = 112
    KEYCODE_MENU = 82
    KEYCODE_NOTIFICATION = 83
    KEYCODE_SEARCH = 84
    KEYCODE_MEDIA_PLAY_PAUSE = 85
    KEYCODE_MEDIA_STOP = 86
    KEYCODE_MEDIA_NEXT = 87
    KEYCODE_MEDIA_PREVIOUS = 88

    @classmethod
    def from_name(cls, name: str) -> int:
        """Convert a key name to its Android keycode integer.

        Accepts both 'HOME' and 'BACK' (which map to KEYCODE_HOME, KEYCODE_BACK)
        and digit names '0'..'9' (which map to KEYCODE_0..KEYCODE_9).
        """
        # Try full KEYCODE_ prefix first
        name_upper = f"KEYCODE_{name.upper()}"
        for member in cls:
            if member.name == name_upper:
                return member.value
        # Try without prefix (allows passing '0', '1', etc.)
        try:
            return int(name)
        except ValueError:
            raise ValueError(f"Unknown key: {name}")


@dataclass
class EmulatorState:
    """Tracks the current state of the emulator connection."""
    connected: bool = False
    webrtc_url: str = ""
    token: str = ""
    screen_width: int = 1080
    screen_height: int = 1920
    device_pixel_ratio: float = 1.0
    last_frame_time: float = 0
    capabilities: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Genymotion WebSocket Client
# ---------------------------------------------------------------------------


class GenymotionWebSocketClient:
    """
    Maintains a WebSocket connection to the Genymotion cloud instance.
    
    The connection flow:
    1. Connect to wss://<webrtcAddress> (the WebRTC signaling server)
    2. Send {"type": "token", "token": "<access_token>"}
    3. Receive SDP offers/answers and ICE candidates
    4. Once WebRTC is established, send input events via the data channel
    
    For simplicity and robustness, this client falls back to WebSocket-only
    mode (useWebsocketAsDataChannel = true) when WebRTC data channels are
    not available, which allows direct input event sending.
    """

    def __init__(self, webrtc_url: str, token: str):
        self.webrtc_url = webrtc_url
        self.token = token
        self.ws_session: Optional[aiohttp.ClientWebSocketResponse] = None
        self.state = EmulatorState(connected=False, webrtc_url=webrtc_url, token=token)
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._response_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        """Establish WebSocket connection to the Genymotion signaling server."""
        if self.ws_session and not self.ws_session.closed:
            return True

        try:
            session = aiohttp.ClientSession(
                headers={"Sec-WebSocket-Protocol": "genymotion-device-web-player"}
            )
            self.ws_session = await session.ws_connect(
                self.webrtc_url,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            )
            self._running = True
            self.state.connected = True
            logger.info(f"Connected to Genymotion at {self.webrtc_url}")

            # Send auth token
            await self.send_event({"type": "token", "token": self.token})

            # Start background listeners
            asyncio.create_task(self._listen_loop())

            return True
        except Exception as e:
            logger.error(f"Failed to connect to Genymotion: {e}")
            self.state.connected = False
            return False

    async def _listen_loop(self):
        """Continuously listen for incoming WebSocket messages."""
        try:
            async for msg in self.ws_session:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_message(data)
                    except json.JSONDecodeError:
                        logger.warning(f"Non-JSON message: {msg.data[:200]}")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {self.ws_session.exception()}")
        except Exception as e:
            logger.error(f"WebSocket listener error: {e}")
        finally:
            self._running = False
            self.state.connected = False
            if self.ws_session and not self.ws_session.closed:
                await self.ws_session.close()

    async def _handle_message(self, data: dict):
        """Route incoming messages to appropriate handlers."""
        msg_type = data.get("type", "")

        if msg_type == "VERSION":
            version = data.get("message", "")
            logger.info(f"Genymotion version: {version}")

        elif msg_type == "CAPABILITIES":
            self.state.capabilities = data.get("message", {})
            logger.debug(f"Capabilities: {self.state.capabilities}")

        elif data.get("sdp"):
            # SDP exchange - in WebRTC mode we'd use this for peer connection
            # For WebSocket-only mode, we just pass through
            logger.debug("Received SDP message")

        elif data.get("candidate"):
            logger.debug("Received ICE candidate")

        elif data.get("connection"):
            logger.info("Connection ready")

        else:
            # Unknown message type - put in event queue for potential event listeners
            await self._event_queue.put(data)

    async def send_event(self, event: dict) -> bool:
        """Send an input event to the Genymotion VM."""
        async with self._lock:
            if not self.ws_session or self.ws_session.closed:
                logger.error("WebSocket not connected, cannot send event")
                return False

            try:
                await self.ws_session.send_json(event)
                return True
            except Exception as e:
                logger.error(f"Failed to send event: {e}")
                return False

    def send_event_sync(self, event: dict) -> bool:
        """Synchronous version - queues the event for async processing."""
        future = asyncio.run_coroutine_threadsafe(
            self.send_event(event), self._get_running_loop()
        )
        return future.result(timeout=5)

    def _get_running_loop(self):
        """Get the running event loop, or find one."""
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            # Fallback - will be set by the async runner
            return asyncio.new_event_loop()

    async def disconnect(self):
        """Cleanly disconnect from the WebSocket."""
        if self.ws_session and not self.ws_session.closed:
            await self.ws_session.close()
        self._running = False
        self.state.connected = False


# ---------------------------------------------------------------------------
# Input Event Helpers
# ---------------------------------------------------------------------------


class InputEvents:
    """Helper class for constructing Genymotion input events."""

    def __init__(self, ws_client: GenymotionWebSocketClient, screen_w: int = 1080, screen_h: int = 1920):
        self.ws = ws_client
        self.screen_w = screen_w
        self.screen_h = screen_h

    @staticmethod
    def normalize_coords(x: int, y: int, screen_w: int, screen_h: int) -> tuple:
        """Convert pixel coordinates to normalized [0, 1] range."""
        nx = max(0.0, min(1.0, x / screen_w))
        ny = max(0.0, min(1.0, y / screen_h))
        return nx, ny

    async def tap(self, x: int, y: int) -> bool:
        """Tap at pixel coordinates (x, y)."""
        nx, ny = self.normalize_coords(x, y, self.screen_w, self.screen_h)
        events = [
            {"type": "MOUSE_PRESS", "x": nx, "y": ny, "source": 0x1002, "button": 1},
            {"type": "MOUSE_RELEASE", "x": nx, "y": ny, "source": 0x1002, "button": 1},
        ]
        for ev in events:
            if not await self.ws.send_event(ev):
                return False
            await asyncio.sleep(0.05)
        return True

    async def tap_normalized(self, x: float, y: float) -> bool:
        """Tap at normalized coordinates (0.0-1.0)."""
        events = [
            {"type": "MOUSE_PRESS", "x": x, "y": y, "source": 0x1002, "button": 1},
            {"type": "MOUSE_RELEASE", "x": x, "y": y, "source": 0x1002, "button": 1},
        ]
        for ev in events:
            if not await self.ws.send_event(ev):
                return False
            await asyncio.sleep(0.05)
        return True

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> bool:
        """Swipe from (x1,y1) to (x2,y2) over duration_ms."""
        nx1, ny1 = self.normalize_coords(x1, y1, self.screen_w, self.screen_h)
        nx2, ny2 = self.normalize_coords(x2, y2, self.screen_w, self.screen_h)

        # Press
        if not await self.ws.send_event({"type": "MOUSE_PRESS", "x": nx1, "y": ny1, "source": 0x1002, "button": 1}):
            return False
        await asyncio.sleep(0.05)

        # Move in steps
        steps = max(2, duration_ms // 20)
        for i in range(1, steps + 1):
            t = i / steps
            mx = nx1 + (nx2 - nx1) * t
            my = ny1 + (ny2 - ny1) * t
            if not await self.ws.send_event({"type": "MOUSE_MOVE", "x": mx, "y": my, "source": 0x1002, "button": 1}):
                return False
            await asyncio.sleep(duration_ms / 1000 / steps)

        # Release
        await self.ws.send_event({"type": "MOUSE_RELEASE", "x": nx2, "y": ny2, "source": 0x1002, "button": 1})
        return True

    async def key_press(self, keycode: int, keychar: str = "") -> bool:
        """Press and release a hardware key."""
        events = [
            {"type": "KEYBOARD_PRESS", "keychar": keychar, "keycode": keycode},
            {"type": "KEYBOARD_RELEASE", "keychar": keychar, "keycode": keycode},
        ]
        for ev in events:
            if not await self.ws.send_event(ev):
                return False
            await asyncio.sleep(0.05)
        return True

    async def key_press_raw(self, keycode: int) -> bool:
        """Press only (no release)."""
        return await self.ws.send_event({"type": "KEYBOARD_PRESS", "keychar": "", "keycode": keycode})

    async def key_release(self, keycode: int) -> bool:
        """Release a previously pressed key."""
        return await self.ws.send_event({"type": "KEYBOARD_RELEASE", "keychar": "", "keycode": keycode})

    async def type_text(self, text: str) -> bool:
        """Type a string of text by sending individual key events."""
        import unicodedata
        for ch in text:
            if ch.isalpha():
                # Android keycodes for letters: A=29, B=30, ...
                base_code = ord(ch.upper()) - ord('A') + 29
                if not await self.key_press(base_code, ch):
                    return False
            elif ch.isdigit():
                # Digits: 0=7, 1=8, ... 9=16 (Android KEYCODE_0..KEYCODE_9)
                try:
                    code = KeyCode.from_name(ch)
                    if not await self.key_press(code, ch):
                        return False
                except ValueError:
                    logger.warning(f"Cannot type digit {ch}")
            else:
                # Handle common special characters via keychar
                char_code = ord(ch)
                if not await self.key_press(char_code, ch):
                    return False
            await asyncio.sleep(0.02)
        return True

    async def scroll(self, x: int, y: int, delta: int) -> bool:
        """Send a scroll event at coordinates."""
        nx, ny = self.normalize_coords(x, y, self.screen_w, self.screen_h)
        return await self.ws.send_event({
            "type": "SCROLL",
            "x": nx, "y": ny,
            "delta": delta  # positive = up, negative = down
        })

    async def get_screenshot(self) -> Optional[bytes]:
        """
        Capture a screenshot from the emulator.
        
        In Genymotion PaaS, screenshots are available via the HTTP API at:
        GET /screenshot/<instance_id>
        
        This returns raw PNG bytes.
        """
        # This would use the Genymotion PaaS HTTP API
        # For the MCP server, we expect screenshots to be delivered via
        # the WebRTC video frame capture
        return None


# ---------------------------------------------------------------------------
# FastAPI HTTP Server
# ---------------------------------------------------------------------------


class EmulatorServer:
    """
    FastAPI server that provides HTTP + WebSocket endpoints for emulator control.
    """

    def __init__(
        self,
        webrtc_url: str = "",
        token: str = "",
        api_token: str = "",
        instance_uuid: str = "",
        base_url: str = "https://api.geny.io/cloud/v1",
        screen_w: int = 1080,
        screen_h: int = 1920,
    ):
        self.webrtc_url = webrtc_url
        self.token = token
        self.api_token = api_token
        self.instance_uuid = instance_uuid
        self.base_url = base_url
        self.screen_w = screen_w
        self.screen_h = screen_h

        self.app = FastAPI(title="Genymotion Emulator Control Server")
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        self.ws_client: Optional[GenymotionWebSocketClient] = None
        self.input_events: Optional[InputEvents] = None
        self._init_routes()

    def _init_routes(self):
        """Set up all HTTP and WebSocket routes."""

        @self.app.get("/")
        async def health():
            return {
                "status": "ok",
                "connected": self.ws_client is not None and self.ws_client.state.connected,
                "webrtc_url": self.webrtc_url,
                "screen": {"width": self.screen_w, "height": self.screen_h},
            }

        @self.app.post("/connect")
        async def connect():
            """Connect to the Genymotion instance via WebSocket."""
            if self.ws_client is None:
                self.ws_client = GenymotionWebSocketClient(self.webrtc_url, self.token)

            if not self.ws_client.state.connected:
                success = await self.ws_client.connect()
                if success:
                    self.input_events = InputEvents(
                        self.ws_client, self.screen_w, self.screen_h
                    )
                    return {"status": "connected", "connected": True}
                else:
                    return {"status": "failed", "connected": False}
            return {"status": "already_connected", "connected": True}

        @self.app.post("/disconnect")
        async def disconnect():
            """Disconnect from the Genymotion instance."""
            if self.ws_client:
                await self.ws_client.disconnect()
            self.ws_client = None
            self.input_events = None
            return {"status": "disconnected", "connected": False}

        @self.app.post("/tap")
        async def api_tap(x: int, y: int):
            """Tap at pixel coordinates."""
            if not self.input_events:
                raise HTTPException(status_code=503, detail="Not connected to emulator")
            result = await self.input_events.tap(x, y)
            return {"success": result, "x": x, "y": y}

        @self.app.post("/tap_normalized")
        async def api_tap_normalized(x: float, y: float):
            """Tap at normalized coordinates (0.0-1.0)."""
            if not self.input_events:
                raise HTTPException(status_code=503, detail="Not connected to emulator")
            result = await self.input_events.tap_normalized(x, y)
            return {"success": result, "x": x, "y": y}

        @self.app.post("/swipe")
        async def api_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300):
            """Swipe from (x1,y1) to (x2,y2)."""
            if not self.input_events:
                raise HTTPException(status_code=503, detail="Not connected to emulator")
            result = await self.input_events.swipe(x1, y1, x2, y2, duration_ms)
            return {"success": result}

        @self.app.post("/key")
        async def api_key(key: str = None, keycode: int = None):
            """Press a hardware key by name or numeric code."""
            if not self.input_events:
                raise HTTPException(status_code=503, detail="Not connected to emulator")
            if key:
                code = KeyCode.from_name(key)
            elif keycode is not None:
                code = keycode
            else:
                raise HTTPException(status_code=400, detail="Must provide key or keycode")
            result = await self.input_events.key_press(code)
            return {"success": result, "keycode": code}

        @self.app.post("/key_press")
        async def api_key_press(key: str = None, keycode: int = None):
            """Press a key (hold down)."""
            if not self.input_events:
                raise HTTPException(status_code=503, detail="Not connected to emulator")
            if key:
                code = KeyCode.from_name(key)
            elif keycode is not None:
                code = keycode
            else:
                raise HTTPException(status_code=400, detail="Must provide key or keycode")
            result = await self.input_events.key_press_raw(code)
            return {"success": result, "keycode": code}

        @self.app.post("/key_release")
        async def api_key_release(key: str = None, keycode: int = None):
            """Release a key."""
            if not self.input_events:
                raise HTTPException(status_code=503, detail="Not connected to emulator")
            if key:
                code = KeyCode.from_name(key)
            elif keycode is not None:
                code = keycode
            else:
                raise HTTPException(status_code=400, detail="Must provide key or keycode")
            result = await self.input_events.key_release(code)
            return {"success": result, "keycode": code}

        @self.app.post("/type")
        async def api_type(text: str):
            """Type text into the emulator."""
            if not self.input_events:
                raise HTTPException(status_code=503, detail="Not connected to emulator")
            result = await self.input_events.type_text(text)
            return {"success": result, "text": text}

        @self.app.post("/scroll")
        async def api_scroll(x: int, y: int, delta: int):
            """Scroll at coordinates."""
            if not self.input_events:
                raise HTTPException(status_code=503, detail="Not connected to emulator")
            result = await self.input_events.scroll(x, y, delta)
            return {"success": result}

        @self.app.post("/send_raw")
        async def api_send_raw(event: dict):
            """Send a raw JSON event to the VM."""
            if not self.ws_client:
                raise HTTPException(status_code=503, detail="Not connected to emulator")
            result = await self.ws_client.send_event(event)
            return {"success": result, "event": event}

        @self.app.get("/screenshot")
        async def api_screenshot():
            """Get a screenshot from the Genymotion PaaS instance via HTTP API."""
            if not self.api_token or not self.instance_uuid:
                raise HTTPException(status_code=503, detail="API token and instance UUID required")

            async with aiohttp.ClientSession() as session:
                headers = {"x-api-token": self.api_token}
                url = f"{self.base_url}/instances/{self.instance_uuid}/screenshot"
                try:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            b64 = content.hex()  # Return as hex for JSON transport
                            return {"success": True, "data": b64, "format": "png"}
                        else:
                            return {"success": False, "error": f"HTTP {resp.status}"}
                except Exception as e:
                    return {"success": False, "error": str(e)}

        @self.app.get("/state")
        async def api_state():
            """Get current emulator state."""
            state = self.ws_client.state if self.ws_client else EmulatorState()
            return {
                "connected": state.connected,
                "webrtc_url": state.webrtc_url,
                "screen_width": state.screen_width,
                "screen_height": state.screen_height,
                "capabilities": state.capabilities,
            }

        @self.app.post("/ws")
        async def ws_endpoint(ws: WebSocket):
            """WebSocket endpoint for real-time browser-side control."""
            await ws.accept()
            try:
                while True:
                    data = await ws.receive_text()
                    try:
                        cmd = json.loads(data)
                    except json.JSONDecodeError:
                        await ws.send_json({"error": "Invalid JSON"})
                        continue

                    action = cmd.get("action", "")
                    result = {"success": False}

                    if action == "tap":
                        result = {"success": await self.input_events.tap(
                            cmd["x"], cmd["y"]
                        )} if self.input_events else result
                    elif action == "swipe":
                        result = {"success": await self.input_events.swipe(
                            cmd["x1"], cmd["y1"], cmd["x2"], cmd["y2"],
                            cmd.get("duration_ms", 300)
                        )} if self.input_events else result
                    elif action == "key":
                        code = KeyCode.from_name(cmd["key"]) if cmd.get("key") else cmd.get("keycode")
                        result = {"success": await self.input_events.key_press(code)} if self.input_events else result
                    elif action == "type":
                        result = {"success": await self.input_events.type_text(cmd["text"])} if self.input_events else result
                    elif action == "send_raw":
                        result = {"success": await self.ws_client.send_event(cmd["event"])} if self.ws_client else result
                    else:
                        result = {"error": f"Unknown action: {action}"}

                    await ws.send_json(result)

            except WebSocketDisconnect:
                logger.info("WebSocket client disconnected")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")


def create_server(
    webrtc_url: str = "",
    token: str = "",
    api_token: str = "",
    instance_uuid: str = "",
    base_url: str = "https://api.geny.io/cloud/v1",
    screen_w: int = 1080,
    screen_h: int = 1920,
) -> EmulatorServer:
    """Factory to create the emulator control server."""
    return EmulatorServer(
        webrtc_url=webrtc_url,
        token=token,
        api_token=api_token,
        instance_uuid=instance_uuid,
        base_url=base_url,
        screen_w=screen_w,
        screen_h=screen_h,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Genymotion Emulator Control Server")
    parser.add_argument("--webrtc-url", required=False, default=os.environ.get("GENY_WEBRTC_URL", ""))
    parser.add_argument("--token", required=False, default=os.environ.get("GENY_TOKEN", ""))
    parser.add_argument("--api-token", required=False, default=os.environ.get("GENY_API_TOKEN", ""))
    parser.add_argument("--instance-uuid", required=False, default=os.environ.get("GENY_INSTANCE_UUID", ""))
    parser.add_argument("--base-url", default="https://api.geny.io/cloud/v1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--screen-w", type=int, default=1080)
    parser.add_argument("--screen-h", type=int, default=1920)
    args = parser.parse_args()

    server = create_server(
        webrtc_url=args.webrtc_url,
        token=args.token,
        api_token=args.api_token,
        instance_uuid=args.instance_uuid,
        base_url=args.base_url,
        screen_w=args.screen_w,
        screen_h=args.screen_h,
    )

    uvicorn.run(server.app, host="0.0.0.0", port=args.port, log_level="info")
