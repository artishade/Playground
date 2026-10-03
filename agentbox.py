"""Agentbox — the AI agent that ships with the terminal.

A terminal hosted on its own is useless without a mind behind it: you want to
type "deploy the site and tail the log", not just get a root prompt. Agentbox
is that mind, and it lives in the terminal host — so **deploying only
`agent_linux/` gives you the shells *and* the agent**, with no Agent_Linux gateway,
no database and no dashboard involved.

    POST /agent/chat        {message, provider?, model?, history?} → the answer
    GET/POST/DELETE /agent/providers   add and choose custom providers
    GET  /agent/models      what a provider can think with

The page that uses all of it — session tabs, a real shell, this agent — is
`web/console.html`, served by the host at `/`.

It talks to any OpenAI-compatible `/v1/chat/completions` endpoint — the free
NovaFree engine, OpenAI, Groq, OpenRouter, a self-hosted vLLM, or a Agent_Linux
gateway's own `/v1`. Providers come from two places:

  environment   AGENT_LINUX_AGENTBOX_BASE_URL / _API_KEY / _MODEL  (id: `env`)
  runtime       POST /agent/providers, saved to a private file next to the
                workspace — so you can add Grok, OpenRouter and a local vLLM
                side by side and pick one per chat

No providers at all means the agent is off, not silently disabled-but-
configured: the chat route answers 503 with `code: agentbox_unconfigured`,
and `/health` says so. The terminal never makes an outbound call nobody asked
for, and an API key is only ever echoed back masked.

Every tool the agent has runs **in a real PTY tab you can watch** — the same
ones `/terminal/pty/sessions` lists — so a task is visible while it happens and
its shell state survives between steps.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config
from . import mcp as mcp_client
from . import plugins as plugin_registry
from . import skills as skill_registry
from . import store as store_module
from .link import TerminalError, current
from .pty import agent_label

log = logging.getLogger("agent_linux.agentbox")

router = APIRouter()

CALL_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=30.0)
MAX_TOOL_OUTPUT = 8000
REPLY_HISTORY = 12          # messages kept per conversation
ENV_PROVIDER_ID = "env"     # the provider built from AGENT_LINUX_AGENTBOX_* variables
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
REGISTRY_LOCK = threading.Lock()   # the registry is a tiny file; keep writes sane

SYSTEM_PROMPT = """You are Agentbox, the AI agent inside a Agent_Linux terminal.

You are working inside a real Linux shell on the user's machine. Every tool
call you make is typed into a live terminal tab the user can watch, so:
- prefer one command at a time, and read the output before deciding what is next;
- state assumptions instead of guessing at paths you have not looked at;
- if a command fails, diagnose it from its real output rather than retrying blindly.

You have the whole toolbox of a root shell: inspect, edit, build, test, deploy.
Do not claim something works until the output says so."""


# --------------------------------------------------------------------------- #
# Tools — every one of them lands in a terminal tab the user can watch
# --------------------------------------------------------------------------- #


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the user's terminal and return its output and exit code.",
            "parameters": _schema({
                "command": {"type": "string", "description": "The command line to run."},
            }, ["command"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": _schema({
                "path": {"type": "string", "description": "Absolute path, or one relative to the workspace."},
                "max_bytes": {"type": "integer", "description": "Truncate after this many bytes (default 20000)."},
            }, ["path"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the workspace.",
            "parameters": _schema({
                "path": {"type": "string", "description": "Absolute path, or one relative to the workspace."},
                "content": {"type": "string", "description": "The full file content."},
            }, ["path", "content"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": "List the terminal tabs currently open, with their labels and working directories.",
            "parameters": _schema({}, []),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Give the user the final answer. Call this when the task is done.",
            "parameters": _schema({
                "summary": {"type": "string", "description": "What you did and what the user should know."},
            }, ["summary"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": ("Load the instructions of one of your skills by name. "
                            "The <skills> index lists what exists; read the matching "
                            "skill before doing a task it covers."),
            "parameters": _schema({
                "name": {"type": "string", "description": "Skill name from the index."},
            }, ["name"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_file",
            "description": "Read a file that ships inside a skill (a script, template or reference doc).",
            "parameters": _schema({
                "name": {"type": "string", "description": "Skill name."},
                "path": {"type": "string", "description": "File path inside the skill."},
            }, ["name", "path"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sql",
            "description": ("Run one SQL statement against the configured database and get rows back. "
                            "Use it for real queries; use run_command only for shell work."),
            "parameters": _schema({
                "sql": {"type": "string", "description": "A single statement — no trailing semicolon."},
                "params": {"type": "array", "description": "Positional parameters ($1, $2 …).",
                           "items": {}},
            }, ["sql"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": ("Open a URL in the shared live browser the user is watching. "
                            "This is a real Chromium page, not a fetch: the user sees every "
                            "step you take and can take over. Returns the page title, the "
                            "final URL and the visible text."),
            "parameters": _schema({
                "url": {"type": "string", "description": "Address to open — `example.com` works."},
                "wait": {"type": "string", "enum": ["domcontentloaded", "load", "networkidle"],
                         "description": "How long to wait before reading the page."},
            }, ["url"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_read",
            "description": ("Read the current page: title, URL and its visible text. "
                            "Call this after opening or clicking to see what changed."),
            "parameters": _schema({
                "max_chars": {"type": "integer", "description": "Truncate the text (default 6000)."},
            }, []),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": ("Click something on the page — by CSS selector, by its visible text, "
                            "or by viewport coordinates. Prefer visible text when you can see it "
                            "in browser_read."),
            "parameters": _schema({
                "selector": {"type": "string", "description": "CSS selector, e.g. 'button[type=submit]'."},
                "text": {"type": "string", "description": "Visible link or button text to click."},
                "x": {"type": "integer", "description": "Viewport x, with y, to click a coordinate."},
                "y": {"type": "integer", "description": "Viewport y, with x."},
            }, []),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into a field (or into whatever has focus), optionally pressing Enter.",
            "parameters": _schema({
                "text": {"type": "string", "description": "What to type."},
                "selector": {"type": "string", "description": "Field selector; omit to type into focus."},
                "submit": {"type": "boolean", "description": "Press Enter afterwards."},
            }, ["text"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll the page, or jump to the bottom, to reach content below the fold.",
            "parameters": _schema({
                "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]},
                "amount": {"type": "integer", "description": "Pixels (default 600)."},
            }, []),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_eval",
            "description": ("Run JavaScript in the page and get the result. The escape hatch for "
                            "scraping: query the DOM and return exactly the data you need. "
                            "Keep it a single expression returning JSON-able data."),
            "parameters": _schema({
                "script": {"type": "string",
                           "description": "An expression or function body, e.g. "
                                          "`[...document.querySelectorAll('h2')].map(h => h.innerText)`"},
            }, ["script"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": ("Capture the current viewport. The frame is saved into the workspace "
                            "as a JPEG and the path is returned, so you can inspect it with the "
                            "shell or hand it to the user."),
            "parameters": _schema({
                "path": {"type": "string", "description": "Where to save it (default: a timestamped file)."},
            }, []),
        },
    },
]


def _resolve_path(raw: str) -> str:
    """A path the agent may touch: absolute, or relative to the workspace."""
    import os

    from .config import build_root

    path = (raw or "").strip()
    if not path:
        return str(build_root())
    return path if os.path.isabs(path) else str(build_root() / path)


async def run_tool(name: str, args: dict[str, Any], label: str) -> Any:
    """One tool call.

    Shell-shaped tools are typed into a real tab the user can watch; extension
    tools (MCP, plugins, skills, sql) go straight to their own endpoint. The
    routing is by name, so a tool the model invented fails loudly instead of
    quietly doing nothing.
    """
    # --- MCP: `mcp__<server>__<tool>` routes back to its server ---------------
    if (route := mcp_client.parse_tool_name(name)) is not None:
        server_id, tool_name = route
        return await mcp_client.call_tool(server_id, tool_name, args)

    # --- Plugins: `plugin__<name>` -------------------------------------------
    if (plugin_name := plugin_registry.parse_tool_name(name)) is not None:
        return await plugin_registry.call_plugin(plugin_name, args)

    if name == "run_command":
        command = str(args.get("command") or "").strip()
        if not command:
            return {"error": "command is required"}
        result = await current().run_command(command, label, config.AGENTBOX_STEP_TIMEOUT)
        if result is None:
            return {"error": "no terminal session is available"}
        output, code = result
        return {"exit_code": code, "output": output[:MAX_TOOL_OUTPUT]}

    if name == "read_file":
        import os

        path = _resolve_path(str(args.get("path") or ""))
        try:
            size = int(args.get("max_bytes") or 20000)
        except (TypeError, ValueError):
            size = 20000
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return {"path": path, "content": fh.read(max(1024, size))}
        except OSError as err:
            return {"error": f"{err.strerror or err} ({path})"}

    if name == "write_file":
        import os

        path = _resolve_path(str(args.get("path") or ""))
        content = args.get("content")
        if not isinstance(content, str):
            return {"error": "content must be a string"}
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as err:
            return {"error": f"{err.strerror or err} ({path})"}
        # The write is visible in the same tab, so the user sees the file land.
        await current().run_command(f"ls -l {path}", label, 30)
        return {"ok": True, "path": path, "bytes": len(content)}

    if name == "list_sessions":
        state = await current().snapshot()
        return {"sessions": [
            {"id": s["id"], "label": s["label"], "cwd": s.get("cwd")}
            for s in (state.get("sessions") or [])
        ]}

    if name == "read_skill":
        return await skill_registry.read(str(args.get("name") or ""))

    if name == "read_skill_file":
        return await skill_registry.read_file(str(args.get("name") or ""),
                                              str(args.get("path") or ""))

    if name == "sql":
        statement = str(args.get("sql") or "").strip()
        if not statement:
            return {"error": "sql is required"}
        params = args.get("params") if isinstance(args.get("params"), list) else None
        try:
            rows = await store_module.get_store().sql(statement, params)
        except store_module.StoreUnavailable as err:
            # A file/supabase store has no SQL — say which backend this is and
            # what to set, because the model can act on that.
            return {"error": str(err), "code": err.code,
                    "store": store_module.describe()}
        except store_module.StoreError as err:
            return {"error": str(err), "code": err.code}
        return {"rows": rows[:200], "count": len(rows)}

    if name.startswith("browser_"):
        return await _browser_tool(name, args)

    return {"error": f"unknown tool: {name}"}


# --------------------------------------------------------------------------- #
# Browser tools — the agent drives the same page the user is watching
# --------------------------------------------------------------------------- #


async def _page_summary(max_chars: int = 6000) -> dict[str, Any]:
    """What the agent needs to decide its next move: where it is and what is here."""
    from .browser import SESSION

    if not SESSION.alive:
        return {"running": False,
                "hint": "the browser is not open — call browser_open with a url first"}
    state = await SESSION.state()
    summary: dict[str, Any] = {
        "running": True,
        "url": state["url"],
        "title": state["title"],
        "viewport": state["viewport"],
    }
    try:
        text = await SESSION.page.inner_text("body")
    except Exception:
        text = ""
    if text:
        limit = max(500, min(40000, int(max_chars or 6000)))
        summary["text"] = text[:limit]
        summary["truncated"] = len(text) > limit
    # Interactive elements, so the model can pick a selector without guessing.
    try:
        elements = await SESSION.page.evaluate(
            "() => [...document.querySelectorAll('a,button,input,textarea,select')]"
            ".slice(0, 60).map(el => ({tag: el.tagName.toLowerCase(),"
            " text: (el.innerText || el.value || el.placeholder || '').trim().slice(0, 80),"
            " name: el.getAttribute('name') || '', id: el.id || '',"
            " type: el.getAttribute('type') || ''}))"
            ".filter(el => el.text || el.name || el.id)"
        )
        if elements:
            summary["elements"] = elements
    except Exception:
        pass
    return summary


async def _browser_tool(name: str, args: dict[str, Any]) -> Any:
    """Route one browser_* tool onto the shared session."""
    from .browser import BrowserError, BrowserUnavailable, SESSION

    try:
        if name == "browser_open":
            url = str(args.get("url") or "").strip()
            if not url:
                return {"error": "url is required"}
            wait = str(args.get("wait") or "domcontentloaded")
            if wait not in ("load", "domcontentloaded", "networkidle"):
                wait = "domcontentloaded"
            state = await SESSION.navigate(url, wait)
            return {"ok": True, "url": state["url"], "title": state["title"],
                    "status_code": state.get("status_code"),
                    "note": "the user can see this page and take over at any time"}

        if name == "browser_read":
            return await _page_summary(int(args.get("max_chars") or 6000))

        if name == "browser_click":
            x, y = args.get("x"), args.get("y")
            state = await SESSION.click(
                str(args.get("selector") or ""),
                int(x) if x is not None else None,
                int(y) if y is not None else None,
                str(args.get("text") or ""),
            )
            summary = await _page_summary(4000)
            summary["clicked"] = state.get("clicked")
            return summary

        if name == "browser_type":
            text = args.get("text")
            if not isinstance(text, str):
                return {"error": "text must be a string"}
            await SESSION.type_text(
                text,
                str(args.get("selector") or ""),
                bool(args.get("submit")),
                True,
            )
            return await _page_summary(4000)

        if name == "browser_scroll":
            await SESSION.scroll(str(args.get("direction") or "down"),
                                 int(args.get("amount") or 600))
            summary = await _page_summary(4000)
            try:
                summary["scroll_y"] = await SESSION.page.evaluate("() => window.scrollY")
            except Exception:
                pass
            return summary

        if name == "browser_eval":
            script = str(args.get("script") or "").strip()
            if not script:
                return {"error": "script is required"}
            if not SESSION.alive:
                return {"error": "the browser is not open — call browser_open first"}
            try:
                result = await SESSION.page.evaluate(script)
            except Exception as err:
                return {"error": f"the script failed: {err.__class__.__name__}: {err}"}
            return {"result": _jsonable(result)}

        if name == "browser_screenshot":
            import time as _time

            from .config import build_root

            raw = await SESSION.frame(70)
            if not raw:
                return {"error": "the page is busy; try again in a moment"}
            target = str(args.get("path") or "").strip()
            if not target:
                target = str(build_root() / f"screenshot-{int(_time.time())}.jpg")
            elif not target.startswith("/"):
                target = str(build_root() / target)
            try:
                with open(target, "wb") as fh:
                    fh.write(raw)
            except OSError as err:
                return {"error": f"cannot write {target}: {err}"}
            return {"ok": True, "path": target, "bytes": len(raw)}
    except BrowserUnavailable as err:
        return {"error": str(err), "code": err.code}
    except BrowserError as err:
        return {"error": str(err), "code": err.code}

    return {"error": f"unknown browser tool: {name}"}


def _jsonable(value: Any) -> Any:
    """Playwright returns JS values; keep the result safe to json.dumps."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_jsonable(item) for item in value[:200]]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in list(value.items())[:200]}
    return str(value)[:2000]


# --------------------------------------------------------------------------- #
# Providers — the agent's custom model endpoints
# --------------------------------------------------------------------------- #


@dataclass
class Provider:
    """One OpenAI-compatible endpoint the agent can think with."""

    id: str
    base_url: str
    api_key: str = ""
    model: str = ""
    label: str = ""
    source: str = "saved"      # `env` | `saved` — where it came from

    def public(self) -> dict:
        """Never the raw key: a saved key is a credential, not a field."""
        return {
            "id": self.id,
            "label": self.label or self.id,
            "base_url": self.base_url,
            "model": self.model or None,
            "source": self.source,
            "api_key": mask_key(self.api_key),
            "has_key": bool(self.api_key),
        }


def mask_key(key: str) -> str:
    """`sk-live-abcd…` → `sk-l…bcd`. Enough to recognise, useless to reuse."""
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:3]}…{text[-3:]}"


def registry_path():
    """Where the saved providers live: private, and beside the workspace."""
    raw = (config.AGENTBOX_PROVIDER_FILE or "").strip()
    if raw:
        from pathlib import Path

        return Path(raw)
    return config.build_root() / ".agentbox-providers.json"


def _env_provider() -> Provider | None:
    base = (config.AGENTBOX_BASE_URL or "").strip()
    if not base:
        return None
    return Provider(id=ENV_PROVIDER_ID, base_url=base.rstrip("/"),
                    api_key=config.AGENTBOX_API_KEY or "",
                    model=config.AGENTBOX_MODEL or "", label="from environment",
                    source="env")


def load_providers() -> dict[str, Provider]:
    """Every provider, keyed by id — the environment one plus the saved ones.

    A saved provider may shadow the environment one by reusing its id (`env`),
    which is how you repoint a baked-in endpoint without touching the deploy.
    """
    found: dict[str, Provider] = {}
    if (env := _env_provider()) is not None:
        found[env.id] = env
    try:
        raw = registry_path().read_text(encoding="utf-8")
        stored = json.loads(raw)
    except (OSError, ValueError):
        return found
    if not isinstance(stored, list):
        return found
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        base = str(entry.get("base_url") or "").strip()
        pid = str(entry.get("id") or "").strip()
        if not base or not ID_RE.match(pid):
            continue
        found[pid] = Provider(id=pid, base_url=base.rstrip("/"),
                              api_key=str(entry.get("api_key") or ""),
                              model=str(entry.get("model") or ""),
                              label=str(entry.get("label") or "")[:40],
                              source="env" if pid == ENV_PROVIDER_ID else "saved")
    return found


def save_providers(providers: list[Provider]) -> None:
    """Persist the saved providers. The env one is never written to disk."""
    path = registry_path()
    with REGISTRY_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {"id": p.id, "label": p.label, "base_url": p.base_url,
             "api_key": p.api_key, "model": p.model}
            for p in providers if p.source != "env"
        ]
        # 0600: this file holds credentials, and the terminal runs as root.
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass


def upsert_provider(*, id: str, base_url: str, api_key: str = "",
                    model: str = "", label: str = "") -> Provider:
    """Add or update one custom provider, keeping every other one as it was."""
    pid = (id or "").strip().lower()
    if not ID_RE.match(pid):
        raise ValueError("id must be 1–40 characters of a–z, 0–9, dot, dash, underscore")
    base = (base_url or "").strip().rstrip("/")
    if not base.lower().startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    clean_model = (model or "").strip()[:120]
    clean_label = (label or "").strip()[:40]

    existing = load_providers()
    previous = existing.get(pid)
    # An empty api_key means "keep the one already stored" — so editing a
    # provider from the page does not have to re-type its credential.
    key = (api_key or "").strip() or (previous.api_key if previous else "")
    provider = Provider(id=pid, base_url=base, api_key=key, model=clean_model,
                        label=clean_label,
                        source="env" if pid == ENV_PROVIDER_ID else "saved")
    keep = [p for p in existing.values() if p.id != pid]
    save_providers([*keep, provider])
    return provider


def remove_provider(pid: str) -> bool:
    """Forget a saved provider. The environment one cannot be removed."""
    if pid == ENV_PROVIDER_ID:
        raise ValueError(f"'{ENV_PROVIDER_ID}' comes from the environment — unset its variables")
    existing = load_providers()
    if pid not in existing:
        return False
    save_providers([p for p in existing.values() if p.id != pid])
    return True


def default_provider() -> Provider | None:
    """Which provider a chat uses when the caller doesn't name one: the env
    one when it exists (that is what the deployer configured), else the first
    saved one — so an added provider is never unreachable."""
    found = load_providers()
    if ENV_PROVIDER_ID in found:
        return found[ENV_PROVIDER_ID]
    return next(iter(found.values()), None)


def resolve_provider(pid: str | None) -> Provider | None:
    found = load_providers()
    if pid:
        return found.get(pid.strip().lower())
    return default_provider()


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


def _client(provider: Provider, transport: Any = None) -> httpx.AsyncClient:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    return httpx.AsyncClient(base_url=provider.base_url, headers=headers,
                             timeout=CALL_TIMEOUT, transport=transport,
                             trust_env=False)


def agentbox_status() -> dict:
    """What `/health` reports about the agent — never a guess."""
    providers = load_providers()
    active = default_provider()
    return {
        "configured": bool(providers),
        "providers": [p.id for p in providers.values()],
        "active": active.id if active else None,
        "model": active.model or None if active else None,
        "endpoint": active.base_url if active else None,
        "max_steps": config.AGENTBOX_MAX_STEPS,
    }


async def _resolve_model(provider: Provider, client: httpx.AsyncClient) -> str:
    """An explicit model wins; otherwise take whatever the endpoint offers."""
    if provider.model:
        return provider.model
    try:
        res = await client.get("/models")
        payload = res.json() if res.content else {}
        listed = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(listed, list) and listed:
            first = listed[0]
            if isinstance(first, dict) and first.get("id"):
                return str(first["id"])
    except Exception:
        pass
    return "gpt-4o-mini"


async def _extra_tools() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """MCP + plugin tools for one turn, plus whatever failed to load.

    Failure is not fatal by design: a dead MCP server is reported to the caller
    and the agent carries on with the tools it does have.
    """
    problems: list[dict[str, Any]] = []
    try:
        mcp_tools = await mcp_client.tool_definitions(problems)
    except Exception as err:                      # noqa: BLE001 — never break a chat
        mcp_tools = []
        problems.append({"server": "*", "error": f"{err.__class__.__name__}: {err}"})
    try:
        plugin_tools = await plugin_registry.tool_definitions()
    except Exception as err:                      # noqa: BLE001
        plugin_tools = []
        problems.append({"plugin": "*", "error": f"{err.__class__.__name__}: {err}"})
    return mcp_tools + plugin_tools, problems


async def _system_prompt() -> str:
    """The base prompt plus the skills index and a note about the database.

    The skills index is the only per-turn cost of a skill library: a name and a
    description each. The bodies stay on disk until `read_skill` asks for one.
    """
    parts = [SYSTEM_PROMPT]
    try:
        index = await skill_registry.index_prompt()
    except Exception:                             # noqa: BLE001
        index = ""
    if index:
        parts.append(index)
    info = store_module.describe()
    if info["backend"] != "file":
        parts.append(
            f"A {info['backend']} database is connected. Use the `sql` tool for real "
            "queries instead of guessing at data through the shell."
        )
    try:
        from .browser_api import _available

        if _available():
            parts.append(
                "A live Chromium browser is available and the user is watching it. "
                "Use browser_open / browser_read / browser_click / browser_type / "
                "browser_scroll / browser_eval for anything on the web — they act on a "
                "real page you can see, and the user can take over at any moment. "
                "Prefer browser_read over curl when you need to see what a page shows, "
                "and browser_eval to extract structured data from the DOM."
            )
    except Exception:                             # noqa: BLE001
        pass
    return "\n\n".join(parts)


async def _chat(client: httpx.AsyncClient, model: str,
                messages: list[dict[str, Any]], tools: bool = True,
                extra_tools: list[dict[str, Any]] | None = None) -> dict:
    body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        # Built-ins first, then whatever MCP and plugins contribute this turn.
        body["tools"] = TOOLS + (extra_tools or [])
    res = await client.post("/chat/completions", json=body)
    if res.status_code >= 400:
        detail = res.text[:300].strip()
        raise RuntimeError(f"model endpoint returned HTTP {res.status_code} ({detail})")
    payload = res.json()
    choices = payload.get("choices") or []
    return choices[0].get("message") or {} if choices else {}


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls") or []
    parsed: list[dict[str, Any]] = []
    for call in calls:
        fn = (call.get("function") or {}) if isinstance(call, dict) else {}
        raw = fn.get("arguments")
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                args = json.loads(raw or "{}")
            except ValueError:
                args = {}
        parsed.append({"id": call.get("id") or "", "name": str(fn.get("name") or ""),
                       "args": args if isinstance(args, dict) else {}})
    return parsed


def agentbox_configured_response() -> JSONResponse | None:
    """The 503 every agent route returns when nobody told it where to think."""
    if load_providers():
        return None
    return JSONResponse(
        {
            "error": "Agentbox has no provider. Set AGENT_LINUX_AGENTBOX_BASE_URL on this "
                     "terminal host, or add one: POST /agent/providers.",
            "code": "agentbox_unconfigured",
        },
        status_code=503,
    )


def _provider_error(pid: str) -> JSONResponse | None:
    """A named provider that doesn't exist is a 404 that lists the real ones."""
    if resolve_provider(pid) is not None:
        return None
    known = ", ".join(load_providers()) or "none configured"
    return JSONResponse(
        {"error": f"no provider '{pid}' (configured: {known})",
         "code": "provider_not_found"},
        status_code=404,
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


@router.post("/chat")
async def chat(request: Request):
    """One turn (or a whole short task) of agent, with its steps visible."""
    if (missing := agentbox_configured_response()) is not None:
        return missing
    body = await _body(request)
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return JSONResponse({"error": "message is required", "code": "bad_request"},
                            status_code=400)

    pid = body.get("provider") if isinstance(body.get("provider"), str) else ""
    if (missing := _provider_error(pid)) is not None:
        return missing
    provider = resolve_provider(pid)
    wanted_model = body.get("model") if isinstance(body.get("model"), str) else ""
    if wanted_model.strip():
        provider = Provider(**{**provider.__dict__, "model": wanted_model.strip()[:120]})

    label = body.get("label") if isinstance(body.get("label"), str) else ""
    label = label or agent_label(message)
    history = body.get("history") if isinstance(body.get("history"), list) else []
    messages: list[dict[str, Any]] = [{"role": "system", "content": await _system_prompt()}]
    for turn in history[-REPLY_HISTORY:]:
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
            messages.append({"role": turn["role"],
                             "content": str(turn.get("content") or "")[:8000]})
    messages.append({"role": "user", "content": message.strip()})

    # Everything the agent gained beyond its five built-ins: MCP servers and
    # plugins. Loaded once per turn, and a failure to load is reported, not fatal.
    extra_tools, problems = await _extra_tools()

    steps: list[dict[str, Any]] = []
    async with _client(provider) as client:
        try:
            model = await _resolve_model(provider, client)
            for _ in range(config.AGENTBOX_MAX_STEPS):
                reply = await _chat(client, model, messages, extra_tools=extra_tools)
                calls = _tool_calls(reply)
                messages.append({k: v for k, v in reply.items() if v is not None})

                if not calls:
                    return {"ok": True, "model": model, "provider": provider.id,
                            "reply": str(reply.get("content") or "").strip(),
                            "steps": steps, "extensions": problems}

                for call in calls:
                    try:
                        result = await run_tool(call["name"], call["args"], label)
                    except TerminalError as err:
                        result = {"error": str(err), "code": err.code}
                    except Exception as err:            # a tool must never kill the run
                        result = {"error": f"{err.__class__.__name__}: {err}"}
                    steps.append({"tool": call["name"], "args": call["args"], "result": result})
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(result)[:MAX_TOOL_OUTPUT]})

                    # `finish` is the agent's own "I am done" button; its summary
                    # is the answer, and the loop ends whether or not the model
                    # stops calling tools.
                    if call["name"] == "finish":
                        summary = str(call["args"].get("summary") or "").strip()
                        return {"ok": True, "model": model, "provider": provider.id,
                                "reply": summary or "Done.", "steps": steps,
                                "extensions": problems}
        except httpx.HTTPError as err:
            return JSONResponse(
                {"error": f"the model endpoint is not answering ({err.__class__.__name__}).",
                 "code": "agentbox_unreachable"},
                status_code=502,
            )
        except RuntimeError as err:
            return JSONResponse({"error": str(err), "code": "agentbox_error"},
                                status_code=502)

    # The step budget ran out — say so honestly instead of pretending it worked.
    return JSONResponse(
        {"error": f"the agent hit its {config.AGENTBOX_MAX_STEPS}-step budget; "
                  "raise AGENT_LINUX_AGENTBOX_MAX_STEPS or ask for something smaller.",
         "code": "agentbox_budget_exhausted", "steps": steps},
        status_code=200,
    )


@router.get("/providers")
async def list_providers():
    """Every custom provider the agent can use, with keys masked."""
    providers = load_providers()
    active = default_provider()
    return {
        "ok": True,
        "active": active.id if active else None,
        "registry": str(registry_path()),
        "providers": [{**p.public(), "active": bool(active and p.id == active.id)}
                      for p in providers.values()],
    }


@router.post("/providers")
async def add_provider(request: Request):
    """Add or update a custom provider: any OpenAI-compatible /v1 endpoint."""
    body = await _body(request)
    try:
        provider = upsert_provider(
            id=str(body.get("id") or ""),
            base_url=str(body.get("base_url") or ""),
            api_key=str(body.get("api_key") or ""),
            model=str(body.get("model") or ""),
            label=str(body.get("label") or ""),
        )
    except ValueError as err:
        return JSONResponse({"error": str(err), "code": "bad_request"}, status_code=400)
    saved = [p.public() for p in load_providers().values()]
    return {"ok": True, "provider": provider.public(), "providers": saved}


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str):
    try:
        removed = remove_provider(provider_id)
    except ValueError as err:
        return JSONResponse({"error": str(err), "code": "bad_request"}, status_code=400)
    if not removed:
        return JSONResponse({"error": f"no provider '{provider_id}'",
                             "code": "provider_not_found"}, status_code=404)
    return {"ok": True, "removed": provider_id,
            "providers": [p.public() for p in load_providers().values()]}


@router.get("/models")
async def models(provider: str = ""):
    """What a provider can think with, straight from that endpoint."""
    if (missing := agentbox_configured_response()) is not None:
        return missing
    if (missing := _provider_error(provider)) is not None:
        return missing
    target = resolve_provider(provider)
    async with _client(target) as client:
        try:
            res = await client.get("/models")
        except httpx.HTTPError as err:
            return JSONResponse({"error": f"unreachable ({err.__class__.__name__})",
                                 "code": "agentbox_unreachable"}, status_code=502)
    payload = res.json() if res.content else {}
    listed = payload.get("data") if isinstance(payload, dict) else None
    return {
        "ok": True,
        "provider": target.id,
        "model": target.model or (listed[0].get("id") if listed else None),
        "models": [m.get("id") for m in (listed or []) if isinstance(m, dict)][:100],
    }