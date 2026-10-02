"""MCP — connect Model Context Protocol servers and give their tools to Agentbox.

The agent's built-in tools are five shell-shaped verbs. MCP is how it gets
everything else: a GitHub server, a Postgres server, a browser, a memory store.
This module is a client, not a framework — enough of the protocol to be useful:

    initialize → notifications/initialized → tools/list → tools/call

Two transports, because both are common in the wild:

    http   Streamable HTTP (JSON-RPC over POST, `Mcp-Session-Id` honoured).
    stdio  A child process speaking JSON-RPC over stdin/stdout — how most
           locally-run MCP servers ship (`npx -y @modelcontextprotocol/…`).

A server is a document in the store, so it can be added from the console, from
curl, or baked in from the environment:

    NOVA_MCP_SERVERS   JSON array of server objects (deployer-baked, id `env`)

Tools are namespaced `mcp__<server>__<tool>` before the model ever sees them, so
two servers can both expose `search` without a collision — and so a tool call
can be routed back to its server by parsing the name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from typing import Any

import httpx

from .store import StoreError, get_store, valid_key

log = logging.getLogger("terminal.mcp")

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "novarouter-terminal", "version": "2.1.0"}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
# `__` is the separator because MCP tool names may contain single underscores,
# and splitting on `_` would be ambiguous.
TOOL_PREFIX = "mcp__"
TOOL_NAME_RE = re.compile(r"^mcp__([a-z0-9._-]+)__([A-Za-z0-9._-]{1,64})$")

CALL_TIMEOUT = 60.0
STDIO_START_TIMEOUT = 25.0
TOOL_CACHE_TTL = 300.0          # seconds a tools/list result is reused

PREFIX = "mcp/"


class McpError(RuntimeError):
    """A server failed to start, answer, or be understood."""

    code = "mcp_error"


# --------------------------------------------------------------------------- #
# Registry — servers live in the store, so any backend can hold them
# --------------------------------------------------------------------------- #


def _doc_key(server_id: str) -> str:
    return f"{PREFIX}{valid_key(server_id)}"


def validate_id(server_id: str) -> str:
    server_id = (server_id or "").strip().lower()
    if not ID_RE.match(server_id):
        raise ValueError("id must be lowercase letters, digits, . _ - (max 40)")
    return server_id


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """One shape for a server, whichever door it came through."""
    transport = str(raw.get("transport") or "").strip().lower()
    url = str(raw.get("url") or "").strip()
    command = str(raw.get("command") or "").strip()
    if not transport:
        transport = "http" if url else ("stdio" if command else "http")

    args = raw.get("args")
    if isinstance(args, str):
        args = shlex.split(args)
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}

    return {
        "id": str(raw.get("id") or ""),
        "label": str(raw.get("label") or raw.get("id") or ""),
        "transport": transport,
        "url": url,
        "command": command,
        "args": [str(a) for a in (args or [])][:32],
        "env": {str(k): str(v) for k, v in list(env.items())[:32]},
        "headers": {str(k): str(v) for k, v in list(headers.items())[:32]},
        "enabled": bool(raw.get("enabled", True)),
        "source": str(raw.get("source") or "saved"),
        "notes": str(raw.get("notes") or "")[:400],
        "added_at": raw.get("added_at") or time.time(),
    }


def _mask(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:3]}…{text[-3:]}"


def _public(server: dict[str, Any], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Never a secret: headers and env are masked, not dropped, so the console
    can show *that* a token is set without ever showing which."""
    def masked(mapping: dict[str, str]) -> dict[str, str]:
        out = {}
        for key, value in mapping.items():
            lowered = key.lower()
            secret = any(word in lowered for word in
                         ("key", "token", "secret", "auth", "password", "bearer", "cookie"))
            out[key] = _mask(value) if secret else value
        return out

    return {
        "id": server["id"],
        "label": server["label"] or server["id"],
        "transport": server["transport"],
        "url": server["url"],
        "command": server["command"],
        "args": server["args"],
        "env": masked(server["env"]),
        "headers": masked(server["headers"]),
        "enabled": server["enabled"],
        "source": server["source"],
        "notes": server["notes"],
        "added_at": server["added_at"],
        "has_secrets": bool(server["headers"] or server["env"]),
        "tools": [{"name": t.get("name"), "description": str(t.get("description") or "")[:200]}
                  for t in (tools or [])],
        "tool_count": len(tools or []),
    }


def _env_servers() -> dict[str, dict[str, Any]]:
    """Servers baked in by the deployer: NOVA_MCP_SERVERS='[{...}]'."""
    raw = (os.environ.get("NOVA_MCP_SERVERS") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as err:
        log.warning("NOVA_MCP_SERVERS is not valid JSON (%s) — ignoring it", err)
        return {}
    if not isinstance(parsed, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            server_id = validate_id(str(item.get("id") or ""))
        except ValueError:
            continue
        out[server_id] = _normalise({**item, "id": server_id, "source": "env"})
    return out


async def load_servers() -> dict[str, dict[str, Any]]:
    """Env servers plus stored ones; a stored server wins on an id clash."""
    servers = _env_servers()
    try:
        docs = await get_store().list(PREFIX)
    except StoreError as err:
        log.warning("mcp: store unavailable (%s) — only env servers are visible", err)
        docs = []
    for doc in docs:
        if not isinstance(doc.get("id"), str):
            continue
        try:
            server = _normalise(doc)
        except Exception:
            continue
        servers[server["id"]] = server
    return servers


async def save_server(server: dict[str, Any]) -> dict[str, Any]:
    if server.get("source") == "env":
        raise ValueError("servers baked in from NOVA_MCP_SERVERS cannot be edited over the API")
    await get_store().put(_doc_key(server["id"]), server)
    return server


async def delete_server(server_id: str) -> bool:
    servers = await load_servers()
    existing = servers.get(validate_id(server_id))
    if existing is None:
        return False
    if existing.get("source") == "env":
        raise ValueError("servers baked in from NOVA_MCP_SERVERS cannot be deleted over the API")
    return await get_store().delete(_doc_key(server_id))


async def resolve(server_id: str) -> dict[str, Any] | None:
    return (await load_servers()).get(validate_id(server_id))


def add_server_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Turn a request body into a storable server, or raise ValueError."""
    server = _normalise({**body, "id": validate_id(str(body.get("id") or ""))})
    if server["transport"] == "http" and not server["url"]:
        raise ValueError("an http server needs a url")
    if server["transport"] == "stdio" and not server["command"]:
        raise ValueError("a stdio server needs a command")
    if server["transport"] not in ("http", "sse", "stdio"):
        raise ValueError("transport must be http, sse or stdio")
    if server["transport"] == "sse":
        server["transport"] = "http"      # same client; the handshake differs
    return server


# --------------------------------------------------------------------------- #
# Tool cache — tools/list is a network round trip; do not repeat it per message
# --------------------------------------------------------------------------- #

_TOOLS: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def cached_tools(server_id: str) -> list[dict[str, Any]] | None:
    hit = _TOOLS.get(server_id)
    if not hit:
        return None
    stamp, tools = hit
    if time.time() - stamp > TOOL_CACHE_TTL:
        _TOOLS.pop(server_id, None)
        return None
    return tools


def remember_tools(server_id: str, tools: list[dict[str, Any]]) -> None:
    _TOOLS[server_id] = (time.time(), tools)


def forget_tools(server_id: str | None = None) -> None:
    if server_id:
        _TOOLS.pop(server_id, None)
    else:
        _TOOLS.clear()


# --------------------------------------------------------------------------- #
# Transport: Streamable HTTP — one POST per request, session id echoed back
# --------------------------------------------------------------------------- #


class HttpSession:
    def __init__(self, server: dict[str, Any]):
        self.url = server["url"]
        self.headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            **{str(k): str(v) for k, v in server["headers"].items()},
        }
        self.session_id = ""
        self.next_id = 1

    def _envelope(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.next_id += 1
        return message

    def _headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    async def request(self, client: httpx.AsyncClient, method: str,
                      params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            res = await client.post(self.url, json=self._envelope(method, params),
                                    headers=self._headers())
        except httpx.HTTPError as err:
            raise McpError(f"{self.url} is unreachable ({err.__class__.__name__})") from err
        if res.status_code >= 400:
            raise McpError(f"{method} failed: HTTP {res.status_code} {res.text[:200].strip()}")
        sid = res.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        return _decode(res)

    async def notify(self, client: httpx.AsyncClient, method: str,
                     params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            await client.post(self.url, json=message, headers=self._headers())
        except httpx.HTTPError:
            pass                          # a notification has no reply to lose


def _decode(res: httpx.Response) -> dict[str, Any]:
    """A Streamable HTTP reply is JSON, or an SSE stream carrying the JSON."""
    if "text/event-stream" in res.headers.get("content-type", ""):
        for line in res.text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(payload, dict) and ("result" in payload or "error" in payload):
                return payload
        raise McpError("the SSE reply carried no JSON-RPC result")
    try:
        payload = res.json()
    except ValueError as err:
        raise McpError(f"the reply was not JSON ({res.text[:120].strip()})") from err
    if not isinstance(payload, dict):
        raise McpError("the reply was not a JSON-RPC object")
    return payload


def _check(payload: dict[str, Any], method: str) -> dict[str, Any]:
    if "error" in payload:
        err = payload["error"] or {}
        detail = err.get("message") if isinstance(err, dict) else str(err)
        raise McpError(f"{method} was refused: {detail}")
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


async def _handshake(session: HttpSession, client: httpx.AsyncClient) -> None:
    _check(await session.request(client, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "clientInfo": CLIENT_INFO,
    }), "initialize")
    await session.notify(client, "notifications/initialized")


async def http_request(server: dict[str, Any], method: str,
                       params: dict[str, Any] | None = None) -> dict[str, Any]:
    timeout = httpx.Timeout(connect=10.0, read=CALL_TIMEOUT, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        session = HttpSession(server)
        await _handshake(session, client)
        return _check(await session.request(client, method, params), method)


# --------------------------------------------------------------------------- #
# Transport: stdio — a child process speaking JSON-RPC on stdin/stdout
# --------------------------------------------------------------------------- #


async def _stdio_call(server: dict[str, Any], method: str,
                      params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start the server, do one round trip, stop it.

    Deliberately short-lived: a supervised long-lived child per server would be
    a second process model to maintain, and the tool cache already keeps the
    start cost off the common path.
    """
    env = {**os.environ, **server["env"]}
    try:
        proc = await asyncio.create_subprocess_exec(
            server["command"], *server["args"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env,
        )
    except (OSError, ValueError) as err:
        raise McpError(f"cannot start '{server['command']}': {err}") from err

    async def send(message: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()

    async def read() -> dict[str, Any]:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), STDIO_START_TIMEOUT)
            if not line:
                stderr = (await proc.stderr.read()).decode(errors="replace")[:300].strip()
                raise McpError(f"the server exited before answering. {stderr}".strip())
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue                  # servers log to stdout too, sadly
            if isinstance(payload, dict) and ("result" in payload or "error" in payload):
                return payload

    try:
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": CLIENT_INFO,
        }})
        _check(await read(), "initialize")
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": method,
                    **({"params": params} if params is not None else {})})
        return _check(await read(), method)
    except asyncio.TimeoutError as err:
        raise McpError(f"'{server['id']}' did not answer within {STDIO_START_TIMEOUT:.0f}s") from err
    finally:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 5)
        except (ProcessLookupError, asyncio.TimeoutError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass


async def call(server: dict[str, Any], method: str,
               params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One MCP method, whichever transport this server uses."""
    if server["transport"] == "stdio":
        return await _stdio_call(server, method, params)
    return await http_request(server, method, params)


# --------------------------------------------------------------------------- #
# The bridge — what agentbox consumes
# --------------------------------------------------------------------------- #


def namespaced(server_id: str, tool_name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", tool_name)[:64]
    return f"{TOOL_PREFIX}{server_id}__{clean}"


def parse_tool_name(name: str) -> tuple[str, str] | None:
    """`mcp__github__create_issue` → ('github', 'create_issue')."""
    match = TOOL_NAME_RE.match(name or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def tool_definition(server_id: str, tool: dict[str, Any]) -> dict[str, Any]:
    """An MCP tool, restated in OpenAI function-calling shape.

    The description carries the server id, because the model needs to know
    *where* a capability comes from when two servers offer similar verbs.
    """
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or not schema.get("type"):
        schema = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": namespaced(server_id, str(tool["name"])),
            "description": f"[{server_id}] {str(tool.get('description') or 'MCP tool')}"[:900],
            "parameters": schema,
        },
    }


async def list_tools(server: dict[str, Any], use_cache: bool = True) -> list[dict[str, Any]]:
    if use_cache:
        hit = cached_tools(server["id"])
        if hit is not None:
            return hit
    result = await call(server, "tools/list", {})
    tools = [t for t in (result.get("tools") or []) if isinstance(t, dict) and t.get("name")]
    remember_tools(server["id"], tools)
    return tools


async def enabled_servers() -> list[dict[str, Any]]:
    return [s for s in (await load_servers()).values() if s.get("enabled")]


async def tool_definitions(problems: list[dict[str, Any]] | None = None,
                           cap: int = 64) -> list[dict[str, Any]]:
    """Every enabled server's tools, in one list, for one chat turn.

    A server that fails to answer is reported in `problems` and skipped: one
    broken MCP server must never take the whole agent offline.
    """
    out: list[dict[str, Any]] = []
    for server in await enabled_servers():
        if len(out) >= cap:
            break
        try:
            tools = await list_tools(server)
        except (McpError, httpx.HTTPError, OSError, ValueError) as err:
            log.warning("mcp: %s unavailable (%s)", server["id"], err)
            if problems is not None:
                problems.append({"server": server["id"], "error": str(err)})
            continue
        for tool in tools:
            out.append(tool_definition(server["id"], tool))
            if len(out) >= cap:
                break
    return out


def _flatten(server_id: str, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """MCP returns content blocks; the model wants text plus an error flag."""
    blocks = result.get("content")
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text") or ""))
            elif kind == "resource":
                resource = block.get("resource") or {}
                parts.append(str(resource.get("text") or resource.get("uri") or ""))
            else:
                parts.append(f"<{kind} content>")
    elif isinstance(blocks, str):
        parts.append(blocks)
    elif result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"]))
    text = "\n".join(p for p in parts if p).strip()
    out: dict[str, Any] = {"server": server_id, "tool": tool_name, "text": text}
    if result.get("isError"):
        out["error"] = text or "the tool reported an error"
    return out


async def call_tool(server_id: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Route one namespaced tool call back to its server."""
    server = await resolve(server_id)
    if server is None:
        return {"error": f"no MCP server '{server_id}'"}
    if not server.get("enabled"):
        return {"error": f"MCP server '{server_id}' is disabled"}
    try:
        result = await call(server, "tools/call", {"name": tool_name, "arguments": args})
    except (McpError, httpx.HTTPError, OSError, ValueError) as err:
        return {"error": str(err), "server": server_id, "tool": tool_name}
    return _flatten(server_id, tool_name, result)


async def probe(server: dict[str, Any]) -> dict[str, Any]:
    """Connect and list tools — what the console's 'test' button calls."""
    started = time.time()
    try:
        tools = await list_tools(server, use_cache=False)
    except (McpError, httpx.HTTPError, OSError, ValueError) as err:
        return {"ok": False, "server": server["id"], "error": str(err),
                "latency_ms": int((time.time() - started) * 1000)}
    return {
        "ok": True,
        "server": server["id"],
        "latency_ms": int((time.time() - started) * 1000),
        "tool_count": len(tools),
        "tools": [{"name": t.get("name"), "description": str(t.get("description") or "")[:200]}
                  for t in tools],
    }


async def status() -> dict[str, Any]:
    """For /health and /agent/extensions — never a secret."""
    servers = await load_servers()
    cached = {sid: len(tools) for sid, (_, tools) in _TOOLS.items()}
    return {
        "servers": len(servers),
        "enabled": sum(1 for s in servers.values() if s.get("enabled")),
        "ids": sorted(servers),
        "cached_tools": cached,
    }


def public(server: dict[str, Any], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Re-exported so the router never reaches into a private name."""
    return _public(server, tools)