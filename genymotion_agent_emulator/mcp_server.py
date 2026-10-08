#!/usr/bin/env python3
"""
Genymotion Emulator MCP Server (stdio transport).

Uses the standard `fastmcp` library for full MCP 2024-11-05 protocol compliance.
Connects to the emulator control server (FastAPI) which in turn drives the
Genymotion Device Web Player.

Run as a stdio MCP server:
    python mcp_server.py

Or as an HTTP MCP server (Streamable HTTP):
    python mcp_server.py --transport http --port 9090
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("geny-mcp")
logging.basicConfig(
    level=os.environ.get("GENY_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class EmulatorConfig:
    """Configuration loaded from environment variables."""
    control_server_url: str = "http://localhost:8080"
    openai_api_key: str = ""
    vision_model: str = "gpt-4o-mini"
    screen_width: int = 1080
    screen_height: int = 1920


def load_config() -> EmulatorConfig:
    return EmulatorConfig(
        control_server_url=os.environ.get("EMULATOR_CONTROL_URL", "http://localhost:8080"),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        vision_model=os.environ.get("VISION_MODEL", "gpt-4o-mini"),
        screen_width=int(os.environ.get("SCREEN_WIDTH", "1080")),
        screen_height=int(os.environ.get("SCREEN_HEIGHT", "1920")),
    )


# ---------------------------------------------------------------------------
# Emulator Client (talks to emulator_control_server.py via HTTP)
# ---------------------------------------------------------------------------


class EmulatorClient:
    """HTTP client that talks to the emulator control server."""

    def __init__(self, config: EmulatorConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def _post(self, path: str, params: dict | None = None, json_body: dict | None = None) -> dict:
        session = await self._session()
        url = f"{self.config.control_server_url}{path}"
        try:
            async with session.post(url, params=params, json=json_body) as resp:
                return await resp.json()
        except aiohttp.ClientError as e:
            return {"success": False, "error": str(e)}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        session = await self._session()
        url = f"{self.config.control_server_url}{path}"
        try:
            async with session.get(url, params=params) as resp:
                return await resp.json()
        except aiohttp.ClientError as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # --- Input operations ---

    async def tap(self, x: int, y: int) -> dict:
        """Tap at pixel coordinates."""
        return await self._post("/tap", params={"x": x, "y": y})

    async def tap_normalized(self, x: float, y: float) -> dict:
        """Tap at normalized coordinates (0.0-1.0)."""
        return await self._post("/tap_normalized", params={"x": x, "y": y})

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> dict:
        """Swipe from (x1,y1) to (x2,y2)."""
        return await self._post("/swipe", params={"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms})

    async def key_press(self, key: Optional[str] = None, keycode: Optional[int] = None) -> dict:
        """Press a hardware key by name or code."""
        params: dict = {}
        if key:
            params["key"] = key
        if keycode is not None:
            params["keycode"] = keycode
        return await self._post("/key", params=params)

    async def type_text(self, text: str) -> dict:
        """Type text into the emulator."""
        return await self._post("/type", params={"text": text})

    async def scroll(self, x: int, y: int, delta: int) -> dict:
        """Scroll at coordinates."""
        return await self._post("/scroll", params={"x": x, "y": y, "delta": delta})

    async def send_raw(self, event: dict) -> dict:
        """Send a raw JSON event to the VM."""
        return await self._post("/send_raw", json_body=event)

    # --- Observation operations ---

    async def take_screenshot_result(self) -> dict:
        """Get screenshot from control server; return dict with hex-encoded data."""
        return await self._get("/screenshot")

    async def take_screenshot_bytes(self) -> Optional[bytes]:
        """Fetch screenshot and return raw PNG bytes (or None)."""
        result = await self.take_screenshot_result()
        if result.get("success") and result.get("data"):
            try:
                return bytes.fromhex(result["data"])
            except (ValueError, TypeError):
                return None
        return None

    async def get_state(self) -> dict:
        """Get current emulator state."""
        return await self._get("/state")

    async def connect(self) -> dict:
        """Connect to emulator via control server."""
        return await self._post("/connect")

    async def disconnect(self) -> dict:
        """Disconnect from emulator."""
        return await self._post("/disconnect")

    # --- AI vision operations ---

    async def analyze_screen(self, prompt: str = "Describe what's on the screen") -> str:
        """Use AI vision to analyze the emulator screen."""
        screenshot = await self.take_screenshot_bytes()
        if screenshot is None:
            return "Unable to capture screenshot for analysis"

        if not self.config.openai_api_key:
            return "No API key configured for screen analysis (set OPENAI_API_KEY)"

        b64_image = base64.b64encode(screenshot).decode("utf-8")

        headers = {
            "Authorization": f"Bearer {self.config.openai_api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.config.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64_image}"},
                        },
                    ],
                }
            ],
            "max_tokens": 4096,
        }

        session = await self._session()
        openai_url = "https://api.openai.com/v1/chat/completions"
        try:
            async with session.post(openai_url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
                else:
                    error_text = await resp.text()
                    return f"Vision API error ({resp.status}): {error_text[:500]}"
        except aiohttp.ClientError as e:
            return f"Vision API request failed: {e}"

    async def find_ui_elements(self) -> list[dict]:
        """Use AI to identify clickable UI elements on screen."""
        result = await self.analyze_screen(
            "Identify all clickable UI elements on this Android screen. "
            "Return a JSON array of objects, each with: label, x, y, width, height "
            "(all coordinates normalized 0.0-1.0), and element_type "
            "(button/text/input/icon). Only return valid JSON, no other text."
        )
        try:
            parsed = json.loads(result)
            if isinstance(parsed, list):
                return parsed
            return []
        except json.JSONDecodeError:
            logger.warning(f"Could not parse UI elements: {result[:200]}")
            return []

    async def execute_task(self, description: str, max_iterations: int = 50) -> str:
        """AI agent loop: analyze screen → plan → act → repeat."""
        steps_log: list[str] = []
        completed = False
        screen_desc = ""

        for i in range(max_iterations):
            # Analyze screen
            screen_desc = await self.analyze_screen(
                f"You are an AI agent controlling an Android mobile emulator. "
                f"The current task is: '{description}'. "
                f"Analyze the current screen and describe what you see: what app is open, "
                f"what elements are visible, what UI state the screen is in."
            )
            steps_log.append(f"[Step {i+1}] Screen: {screen_desc}")
            logger.info(f"Task step {i+1}: {screen_desc[:100]}")

            # Find UI elements
            elements = await self.find_ui_elements()
            if elements:
                steps_log.append(f"[Step {i+1}] Found {len(elements)} UI elements")

            # AI decision — what action to take next
            action_prompt = (
                f"You are an AI agent controlling an Android mobile emulator via a screen.\n"
                f"Task: '{description}'\n\n"
                f"Screen description: {screen_desc}\n\n"
                f"UI Elements found:\n"
            )
            for el in elements[:15]:
                action_prompt += (
                    f"- {el.get('label', '?')} ({el.get('element_type', '?')}) "
                    f"at ({el.get('x', 0):.3f}, {el.get('y', 0):.3f})\n"
                )

            action_prompt += (
                "\nWhat should you do next? Return ONLY a JSON object, no other text:\n"
                '{"action": "tap", "x": 0.5, "y": 0.5}\n'
                '{"action": "type", "text": "hello"}\n'
                '{"action": "swipe", "x1": 0.5, "y1": 0.5, "x2": 0.5, "y2": 0.1}\n'
                '{"action": "key", "key": "BACK"}\n'
                '{"action": "wait"}\n'
                '{"action": "done", "result": "Task complete"}\n'
            )

            action_str = await self.analyze_screen(action_prompt)

            try:
                action = json.loads(action_str)
            except json.JSONDecodeError:
                if "```json" in action_str:
                    snippet = action_str.split("```json")[1].split("```")[0]
                    action = json.loads(snippet)
                elif "```" in action_str:
                    snippet = action_str.split("```")[1].split("```")[0]
                    action = json.loads(snippet)
                else:
                    action = {"action": "wait"}

            act_type = action.get("action", "wait")

            if act_type == "done":
                completed = True
                result = action.get("result", "Task completed")
                steps_log.append(f"[Step {i+1}] Done: {result}")
                break

            elif act_type == "tap":
                x, y = action.get("x", 0.5), action.get("y", 0.5)
                if x > 1.0 or y > 1.0:
                    x, y = x / self.config.screen_width, y / self.config.screen_height
                steps_log.append(f"[Step {i+1}] Tap at ({x:.3f}, {y:.3f})")
                await self.tap_normalized(x, y)

            elif act_type == "type":
                text = action.get("text", "")
                steps_log.append(f"[Step {i+1}] Type: {text}")
                await self.type_text(text)

            elif act_type == "swipe":
                x1, y1 = action.get("x1", 0.5), action.get("y1", 0.5)
                x2, y2 = action.get("x2", 0.5), action.get("y2", 0.1)
                coords = [x1, y1, x2, y2]
                if any(c > 1.0 for c in coords):
                    x1, y1 = x1 / self.config.screen_width, y1 / self.config.screen_height
                    x2, y2 = x2 / self.config.screen_width, y2 / self.config.screen_height
                steps_log.append(f"[Step {i+1}] Swipe ({x1:.3f},{y1:.3f})->({x2:.3f},{y2:.3f})")
                abs_x1 = int(x1 * self.config.screen_width)
                abs_y1 = int(y1 * self.config.screen_height)
                abs_x2 = int(x2 * self.config.screen_width)
                abs_y2 = int(y2 * self.config.screen_height)
                await self.swipe(abs_x1, abs_y1, abs_x2, abs_y2)

            elif act_type == "key":
                key = action.get("key", "BACK")
                steps_log.append(f"[Step {i+1}] Key: {key}")
                await self.key_press(key=key)

            elif act_type == "wait":
                steps_log.append(f"[Step {i+1}] Wait 2s")
                await asyncio.sleep(2)

            await asyncio.sleep(0.5)

        if not completed:
            steps_log.append(f"[Final] Task did not complete after {max_iterations} iterations.")
        else:
            steps_log.append(f"[Final] Task complete.")

        return "\n".join(steps_log)


# ---------------------------------------------------------------------------
# MCP Server (using fastmcp for spec compliance)
# ---------------------------------------------------------------------------

config = load_config()
mcp = FastMCP("genymotion-emulator", version="1.0.0")
client = EmulatorClient(config)


@mcp.tool()
async def geny_connect(webrtc_url: str = "", token: str = "") -> str:
    """Connect to the Genymotion emulator instance. Must be called before other tools."""
    if webrtc_url:
        client.config.control_server_url = webrtc_url
    result = await client.connect()
    return json.dumps(result)


@mcp.tool()
async def geny_disconnect() -> str:
    """Disconnect from the emulator."""
    result = await client.disconnect()
    return json.dumps(result)


@mcp.tool()
async def geny_tap(x: int, y: int) -> str:
    """Tap at pixel coordinates (x, y) on the emulator screen."""
    result = await client.tap(x, y)
    return json.dumps(result)


@mcp.tool()
async def geny_tap_normalized(x: float, y: float) -> str:
    """Tap at normalized coordinates (0.0-1.0) on the emulator screen."""
    result = await client.tap_normalized(x, y)
    return json.dumps(result)


@mcp.tool()
async def geny_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
    """Swipe from (x1, y1) to (x2, y2) over duration_ms milliseconds."""
    result = await client.swipe(x1, y1, x2, y2, duration_ms)
    return json.dumps(result)


@mcp.tool()
async def geny_key(key: str = "", keycode: Optional[int] = None) -> str:
    """Press an Android hardware key by name (e.g. BACK, HOME, VOLUME_UP, ENTER, DEL, MENU) or numeric code."""
    result = await client.key_press(key=key or None, keycode=keycode)
    return json.dumps(result)


@mcp.tool()
async def geny_type(text: str) -> str:
    """Type text into the emulator (e.g., to fill form fields)."""
    result = await client.type_text(text)
    return json.dumps(result)


@mcp.tool()
async def geny_scroll(x: int, y: int, delta: int) -> str:
    """Scroll at coordinates (x, y). Positive delta scrolls up, negative down."""
    result = await client.scroll(x, y, delta)
    return json.dumps(result)


@mcp.tool()
async def geny_send_raw(event: dict) -> str:
    """Send a raw JSON event to the Genymotion VM (for advanced users)."""
    result = await client.send_raw(event)
    return json.dumps(result)


@mcp.tool()
async def geny_screenshot() -> str:
    """Take a screenshot of the emulator screen. Returns JSON with base64-encoded PNG data."""
    result = await client.take_screenshot_result()
    return json.dumps(result)


@mcp.tool()
async def geny_state() -> dict:
    """Get the current emulator state: connection status, screen dimensions, capabilities."""
    return await client.get_state()


@mcp.tool()
async def geny_analyze(prompt: str = "What's on this Android screen?") -> str:
    """Analyze the current emulator screen using AI vision. Returns a text description."""
    return await client.analyze_screen(prompt)


@mcp.tool()
async def geny_find_elements() -> str:
    """Identify clickable UI elements on screen via AI vision. Returns JSON array with labels, coordinates (normalized 0-1), and types."""
    elements = await client.find_ui_elements()
    return json.dumps({"elements": elements})


@mcp.tool()
async def geny_execute_task(description: str, max_iterations: int = 50) -> str:
    """Execute a high-level task on the emulator using an AI agent loop, e.g. 'Open Chrome and search for Genymotion documentation'."""
    return await client.execute_task(description, max_iterations)


# --- Resources ---


@mcp.resource("geny://screenshot")
async def screenshot_resource() -> bytes:
    """Current emulator screenshot as PNG bytes."""
    result = await client.take_screenshot_bytes()
    if result is None:
        return b""
    return result


@mcp.resource("geny://state")
async def state_resource() -> str:
    """Current emulator connection state and capabilities as JSON."""
    return json.dumps(await client.get_state())


# --- Prompts ---


@mcp.prompt()
async def geny_task_prompt(task: str, context: str = "") -> str:
    """Generate a prompt for executing a task on the emulator."""
    prompt = f"You are controlling an Android emulator. Task: '{task}'."
    if context:
        prompt += f"\n\nCurrent screen context: {context}"
    return prompt


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Genymotion Emulator MCP Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="Transport: stdio (default) or http (Streamable HTTP)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9090, help="HTTP port (default: 9090)")
    args = parser.parse_args()

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()