# Genymotion Web Emulator MCP Server

## Overview

An MCP server that lets AI agents control a **Genymotion Device Web Player** instance — a self-hosted Android emulator accessible via WebRTC. Agents can tap, swipe, type, press keys, take screenshots, run vision analysis, and execute high-level tasks (e.g., "open Chrome and search for X").

## Architecture

```
AI Agent (Agentbox)
    │ MCP (stdio/jsonrpc)
    ▼
┌─────────────────────────────────────────┐
│  mcp_server.py  (fastmcp, stdio)        │
│  — geny_tap, geny_swipe, geny_type, ...  │
│  — geny_analyze (vision loop)            │
└──────────────┬──────────────────────────┘
              │ HTTP/JSON
              ▼
┌─────────────────────────────────────────┐
│  emulator_control_server.py (FastAPI)    │
│  — WebSocket → Genymotion signaling      │
│  — Input event dispatching               │
│  — Screenshot via PaaS HTTP API          │
└──────────────┬──────────────────────────┘
              │ WebSocket
              ▼
┌─────────────────────────────────────────┐
│  Genymotion Device Web Player            │
│  (WebRTC signaling → Android VM)         │
└─────────────────────────────────────────┘
```

## Genymotion WebSocket Protocol

Input events are sent as JSON over the WebSocket connection to the Genymotion signaling server:

```json
{"type": "MOUSE_PRESS", "x": 0.5, "y": 0.5, "source": 0x1002, "button": 1}
{"type": "MOUSE_RELEASE", "x": 0.5, "y": 0.5, "source": 0x1002, "button": 1}
{"type": "MOUSE_MOVE", "x": 0.6, "y": 0.5, "source": 0x1002, "button": 1}
{"type": "KEYBOARD_PRESS", "keychar": "a", "keycode": 97}
{"type": "KEYBOARD_RELEASE", "keychar": "a", "keycode": 97}
```

**Coordinates** are normalized (0.0–1.0) relative to screen dimensions.
**Keycodes** are Android key codes (HOME=3, BACK=4, VOLUME_UP=24, ENTER=66, DEL=112, etc.).

## Authentication

1. Start a Genymotion instance and get its UUID from the [Genymotion Cloud Console](https://cloud.geny.io).
2. Obtain a WebSocket access token:
   ```bash
   curl -H "X-API-Token: $GENY_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"adb_serial":"$INSTANCE_UUID"}' \
        https://api.geny.io/cloud/v1/instances/$INSTANCE_UUID/access-token
   ```
3. Extract `webrtc_address` from the response.
4. Pass the token as `GENY_TOKEN` and the UUID as `GENY_INSTANCE_UUID`.

## Installation

### Option A: Run as a stdio MCP server (recommended for Agentbox)

The MCP server connects to the emulator control server over HTTP. Start the control server first:

```bash
# Terminal 1: control server (manages WebSocket connection to Genymotion)
GENY_TOKEN=... GENY_API_TOKEN=... GENY_INSTANCE_UUID=... \
  python emulator_control_server.py \
  --token "$GENY_TOKEN" \
  --webrtc-url "wss://..." \
  --api-token "$GENY_API_TOKEN" \
  --instance-uuid "$GENY_INSTANCE_UUID"
```

Then register the MCP server with Agentbox via environment variable or store:

```bash
# Set env var before starting Agentbox
export AGENT_LINUX_MCP_SERVERS='[
  {
    "id": "geny",
    "label": "Genymotion Emulator",
    "transport": "stdio",
    "command": "python",
    "args": ["/app/genymotion_agent_emulator/mcp_server.py"],
    "env": {
      "EMULATOR_CONTROL_URL": "http://localhost:8080",
      "OPENAI_API_KEY": "sk-...",
      "VISION_MODEL": "gpt-4o-mini",
      "SCREEN_WIDTH": "1080",
      "SCREEN_HEIGHT": "1920"
    }
  }
]'
```

Or via the extensions API:
```bash
curl -X POST http://localhost:8080/agent/extensions/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "id": "geny",
    "label": "Genymotion Emulator",
    "transport": "stdio",
    "command": "python",
    "args": ["/app/genymotion_agent_emulator/mcp_server.py"],
    "env": {
      "EMULATOR_CONTROL_URL": "http://localhost:8080"
    }
  }'
```

### Option B: Docker

```bash
docker build -t geny-mcp .
docker run -it --rm -p 8080:8080 \
  -e GENY_TOKEN=... \
  -e GENY_API_TOKEN=... \
  -e GENY_INSTANCE_UUID=... \
  -e OPENAI_API_KEY=... \
  geny-mcp
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `geny_connect` | Connect to emulator via control server |
| `geny_disconnect` | Disconnect from emulator |
| `geny_tap(x, y)` | Tap at pixel coordinates |
| `geny_tap_normalized(x, y)` | Tap at normalized coords (0.0-1.0) |
| `geny_swipe(x1, y1, x2, y2, duration_ms)` | Swipe gesture |
| `geny_key(key, keycode)` | Press hardware key (BACK, HOME, etc.) |
| `geny_type(text)` | Type text (form filling) |
| `geny_scroll(x, y, delta)` | Scroll at coordinates |
| `geny_screenshot()` | Screenshot as base64 PNG |
| `geny_state()` | Emulator connection state |
| `geny_analyze(prompt)` | AI vision analysis of screen |
| `geny_find_elements()` | AI identifies UI elements |
| `geny_execute_task(description, max_iterations)` | AI agent loop for complex tasks |
| `geny_send_raw(event)` | Send raw JSON event to VM |

## Example Usage

Once registered, the AI agent can call these tools directly:

```
geny_tap(x=540, y=960)    # tap center of 1080x1920 screen
geny_type(text="hello@example.com")
geny_key(key="ENTER")
geny_execute_task(description="Open Chrome and search for Genymotion documentation")
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GENY_TOKEN` | "" | Genymotion WebSocket access token |
| `GENY_API_TOKEN` | "" | Genymotion PaaS API token |
| `GENY_INSTANCE_UUID` | "" | Genymotion instance UUID |
| `GENY_WEBRTC_URL` | "" | WebSocket URL of the instance |
| `GENY_BASE_URL` | `https://api.geny.io/cloud/v1` | Genymotion API base |
| `GENY_LOG_LEVEL` | `INFO` | Log level |
| `EMULATOR_CONTROL_URL` | `http://localhost:8080` | Control server URL |
| `OPENAI_API_KEY` | "" | Vision API key |
| `VISION_MODEL` | `gpt-4o-mini` | Vision model for analysis |
| `SCREEN_WIDTH` | `1080` | Virtual screen width |
| `SCREEN_HEIGHT` | `1920` | Virtual screen height |