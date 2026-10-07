"""Agentbox — the AI agent that ships with the terminal.

A terminal hosted on its own is useless without a mind behind it: you want to
type "deploy the site and tail the log", not just get a root prompt. Agentbox
is that mind, and it lives in the terminal host — so **deploying only
`agent_linux/` gives you the shells *and* the agent**, with no Agent_Linux gateway,
no database and no dashboard involved.

    POST /agent/chat        {message, provider?, model?, system?, history?} → the answer
    POST /agent/images/generate   {prompt, provider?, model?, size?} → images
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
import socket
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
    {
        "type": "function",
        "function": {
            "name": "ssh_run",
            "description": ("Run a command on a saved SSH host. Uses the stored key or password, "
                            "and returns the remote output. For an interactive session the user "
                            "opens the host as a tab instead — this is for one-off commands."),
            "parameters": _schema({
                "host": {"type": "string", "description": "Saved host name, e.g. `prod-web`."},
                "command": {"type": "string", "description": "The command to run on the remote host."},
                "timeout_s": {"type": "integer", "description": "Give up after this many seconds (default 60)."},
            }, ["host", "command"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_hosts",
            "description": "List the saved SSH hosts and keys available on this machine.",
            "parameters": _schema({}, []),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "accounts",
            "description": ("List the saved cloud/database accounts (Postgres, Supabase, Neon, "
                            "Cloudflare, Google, GitHub). Secrets are masked — use account_env "
                            "to actually use one."),
            "parameters": _schema({
                "provider": {"type": "string", "description": "Optional: only this provider."},
            }, []),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "account_env",
            "description": ("Run a shell command with a saved account's credentials in its "
                            "environment — e.g. a Cloudflare account gives the command "
                            "CLOUDFLARE_API_TOKEN. The secret is never printed, only exported "
                            "into that process."),
            "parameters": _schema({
                "provider": {"type": "string", "description": "postgres, supabase, cloudflare, google, github…"},
                "name": {"type": "string", "description": "Account name, e.g. `work`."},
                "command": {"type": "string", "description": "Command to run with those variables set."},
            }, ["provider", "name", "command"]),
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

    if name in ("ssh_run", "ssh_hosts"):
        return await _ssh_tool(name, args)

    if name in ("accounts", "account_env"):
        return await _account_tool(name, args)

    return {"error": f"unknown tool: {name}"}


# --------------------------------------------------------------------------- #
# SSH and account tools — credentials the agent may use but never sees
# --------------------------------------------------------------------------- #


async def _ssh_tool(name: str, args: dict[str, Any]) -> Any:
    """SSH from the agent's side.

    Note what is *not* here: the agent can run a command on a host, but it cannot
    read a private key or a saved password. `ssh_run` builds the same argv the
    interactive tab would and hands the secret to that child process only, so a
    key never reaches the model's context.
    """
    from . import ssh as ssh_mod

    try:
        if name == "ssh_hosts":
            hosts = await ssh_mod.list_hosts()
            keys = await ssh_mod.list_keys()
            return {
                "hosts": [ssh_mod.public_host(h) for h in hosts],
                "keys": [{"name": k.get("name"), "fingerprint": k.get("fingerprint"),
                          "type": k.get("type")} for k in keys],
                "note": "secrets are stored encrypted and are never returned here",
            }

        host_name = str(args.get("host") or "").strip()
        command = str(args.get("command") or "").strip()
        if not host_name or not command:
            return {"error": "host and command are required"}
        try:
            timeout = max(5, min(900, int(args.get("timeout_s") or 60)))
        except (TypeError, ValueError):
            timeout = 60

        host = await ssh_mod.get_host(host_name, reveal=True)
        if host is None:
            known = ", ".join(h.get("name", "") for h in await ssh_mod.list_hosts()) or "none"
            return {"error": f"no ssh host '{host_name}' (saved: {known})"}

        import asyncio
        import time as _time

        from . import secrets as secrets_mod

        cleanup: list[Any] = []
        started = _time.time()
        try:
            key_path = await ssh_mod._materialise_key(host, cleanup)
            argv = ssh_mod.connection_argv(
                host, key_path,
                ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"],
                command,
            )
            environment = ssh_mod._ssh_env(host, cleanup)
        except Exception:
            for path in cleanup:
                ssh_mod._shred(path)
            raise

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return {"error": f"the remote command did not finish within {timeout}s",
                        "host": host_name}
            code = proc.returncode
        finally:
            # The temporary key and askpass exist only for this command.
            for path in cleanup:
                ssh_mod._shred(path)

        stdout = out.decode(errors="replace")
        stderr = err.decode(errors="replace")
        result: dict[str, Any] = {
            "host": host_name,
            "target": ssh_mod.public_host(host)["target"],
            "exit_code": code,
            "output": stdout[:MAX_TOOL_OUTPUT],
            "latency_ms": int((_time.time() - started) * 1000),
        }
        if code != 0:
            result["error"] = ssh_mod._explain(stderr or stdout)
            result["stderr"] = stderr[:2000]
        return result
    except Exception as err:                       # noqa: BLE001
        return {"error": f"{err.__class__.__name__}: {err}", "code": "ssh_error"}


async def _account_tool(name: str, args: dict[str, Any]) -> Any:
    """Accounts from the agent's side — listing is masked, use is env-only."""
    from . import accounts as accounts_mod

    try:
        if name == "accounts":
            provider = str(args.get("provider") or "").strip().lower()
            docs = await accounts_mod.load_all()
            if provider:
                docs = [d for d in docs if d.get("provider") == provider]
            return {
                "accounts": [accounts_mod.public(d) for d in docs],
                "active": {d["provider"]: d["name"] for d in docs if d.get("active")},
                "note": "secrets are masked; use account_env to run something with one",
            }

        provider = str(args.get("provider") or "").strip().lower()
        account = str(args.get("name") or "").strip().lower()
        command = str(args.get("command") or "").strip()
        if not provider or not account or not command:
            return {"error": "provider, name and command are required"}

        try:
            values = await accounts_mod.env_for(provider, account)
        except accounts_mod.AccountError as err:
            return {"error": str(err)}

        from .config import build_root

        # Exported as an inline `env KEY=… cmd` prefix would leak the secret into
        # the terminal's scrollback and into `ps`. Instead the values are written
        # to a 0600 file that the command sources and deletes, so the transcript
        # shows the command and never the credential.
        import os
        import stat
        import uuid

        path = build_root() / f".env-{uuid.uuid4().hex}"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for key, value in values.items():
                # Single-quoted and escaped, so a value containing a quote cannot
                # break out of the assignment and run something else.
                safe = str(value).replace("'", "'\\''")
                fh.write(f"export {key}='{safe}'\n")
        try:
            wrapped = f". {path} && {command}; __rc=$?; rm -f {path}; exit $__rc"
            # Through the link, like every other shell-shaped tool: the command
            # runs in a real tab the user is watching.
            result_pair = await current().run_command(wrapped, "account", config.AGENTBOX_STEP_TIMEOUT)
            if result_pair is None:
                return {"error": "no terminal session is available"}
            output, code = result_pair
        finally:
            try:
                path.unlink()
            except OSError:
                pass

        result: dict[str, Any] = {
            "provider": provider,
            "account": account,
            "variables": sorted(values),
            "exit_code": code,
            "output": (output or "")[:MAX_TOOL_OUTPUT],
        }
        if code != 0:
            result["error"] = "the command exited non-zero — see output"
        return result
    except Exception as err:                       # noqa: BLE001
        return {"error": f"{err.__class__.__name__}: {err}", "code": "account_error"}


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


# --------------------------------------------------------------------------- #
# Privacy mode — local endpoints only, by construction
# --------------------------------------------------------------------------- #

# Local engines that keep every prompt on this machine (or its LAN), by port.
LOCAL_ENGINE_PORTS = (11434, 8000, 1234, 8080, 5000, 9997, 9998)

_local_lan_cache: list[str] = []


def _this_machine_addrs() -> list[str]:
    """Every IP this host answers on — 127.0.0.1 plus any LAN addresses."""
    global _local_lan_cache
    if _local_lan_cache:
        return _local_lan_cache
    addrs: list[str] = ["127.0.0.1", "::1", "localhost"]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, proto=socket.IPPROTO_TCP):
            addr = (info[4] or ("",))[0]
            if addr and addr not in addrs:
                addrs.append(addr)
    except OSError:
        pass
    # A route-based probe: where do we egress if we talk to the internet?
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.settimeout(1.0)
            probe.connect(("8.8.8.8", 80))
            addrs.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    _local_lan_cache = addrs
    return addrs


def is_local_endpoint(base_url: str) -> bool:
    """True when an OpenAI-compatible endpoint lives on this machine or its LAN.

    Loopback is always local. Everything else must resolve to one of this
    host's own addresses or sit in a private range (10/8, 172.16/12,
    192.168/16) — a typical `http://192.168.1.20:11434` Ollama box counts,
    api.openai.com does not. Privacy mode trusts this, and nothing else.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse((base_url or "").strip())
        host = (parsed.hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in ("localhost", "::1", "127.0.0.1", "::ffff:127.0.0.1"):
        return True
    if host in _this_machine_addrs():
        return True
    try:
        addr = socket.inet_aton(host)
        octets = addr[0], addr[1]
    except OSError:
        return False                     # a name that is not an IP: resolve it
    first, second = octets[0], octets[1]
    if first == 10 or first == 192 and second == 168 or first == 172 and 16 <= second <= 31:
        return True
    return False


def assert_privacy(base_url: str) -> None:
    """Raise if privacy mode is on and `base_url` would leave this machine.

    Blocking here — at the provider boundary, before any message is built —
    means the guarantee holds for every route and every tool, and there is no
    per-call check that a future edit can forget to add.
    """
    if not config.AGENTBOX_PRIVACY_MODE:
        return
    if is_local_endpoint(base_url):
        return
    raise RuntimeError(
        "privacy mode is on (AGENT_LINUX_PRIVACY_MODE=1) and this provider is not a "
        "local endpoint — point Agentbox at a local engine (Ollama, vLLM, LM Studio, "
        "llama.cpp) on this machine or your LAN instead."
    )


# --------------------------------------------------------------------------- #
# Local engine discovery — find the engines already running on this machine
# --------------------------------------------------------------------------- #

# port → (engine name, the path that proves it). Every one speaks
# OpenAI-compatible /v1/models today, which is all Agentbox needs to work.
LOCAL_ENGINE_PORTS: tuple[tuple[int, str, str], ...] = (
    (11434, "Ollama", ""),
    (8000,  "vLLM", ""),
    (1234,  "LM Studio", ""),
    (1337,  "Jan", ""),
    (8080,  "llama.cpp", ""),
    (4000,  "LiteLLM", ""),
    (5000,  "text-gen-webui / LocalAI", ""),
    (9997,  "LocalAI", ""),
)


async def _probe_local_engine(port: int, engine: str, _: str) -> dict[str, Any] | None:
    """Is something OpenAI-compatible listening on this loopback port?"""
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), trust_env=False) as client:
            res = await client.get(f"{base}/v1/models")
            payload = res.json() if res.content else {}
    except Exception:                              # noqa: BLE001 — a closed port is normal
        return None
    models: list[str] = []
    if isinstance(payload, dict):
        listed = payload.get("data")
        if isinstance(listed, list):
            models = [str(m.get("id")) for m in listed if isinstance(m, dict) and m.get("id")]
    if not models and port == 11434:
        # Ollama before its OpenAI shim, or with it disabled: /api/tags is native.
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), trust_env=False) as client:
                res = await client.get(f"{base}/api/tags")
                payload = res.json() if res.content else {}
            models = [str(m.get("name") or m.get("model"))
                      for m in (payload.get("models") or []) if isinstance(m, dict)]
        except Exception:                          # noqa: BLE001
            pass
    if not models:
        return None
    return {"port": port, "engine": engine, "base_url": f"{base}/v1",
            "models": models[:50]}


async def detect_local_engines() -> list[dict[str, Any]]:
    """Every local engine answering on a well-known port, models included."""
    import asyncio

    found = await asyncio.gather(*(
        _probe_local_engine(port, engine, extra)
        for port, engine, extra in LOCAL_ENGINE_PORTS
    ))
    return [engine for engine in found if engine]


def _public_probe(engine: dict[str, Any]) -> dict[str, Any]:
    """What the console sees: no secrets, just enough to offer a one-click add."""
    return {
        "engine": engine["engine"],
        "base_url": engine["base_url"],
        "models": engine["models"][:20],
        "configured": any(
            p.base_url.rstrip("/") == engine["base_url"].rstrip("/")
            for p in load_providers().values()
        ),
    }


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
    status: dict[str, Any] = {
        "configured": bool(providers),
        "providers": [p.id for p in providers.values()],
        "active": active.id if active else None,
        "model": active.model or None if active else None,
        "endpoint": active.base_url if active else None,
        "max_steps": config.AGENTBOX_MAX_STEPS,
        "privacy_mode": bool(config.AGENTBOX_PRIVACY_MODE),
        "local_only": all(is_local_endpoint(p.base_url) for p in providers.values()) if providers else None,
        "system_prompt": "custom" if (config.AGENTBOX_SYSTEM_PROMPT or (config.build_root() / ".agentbox-system-prompt").is_file()) else "default",
    }
    return status


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


async def _system_prompt(override: str = "") -> str:
    """The base prompt plus the skills index and a note about the database.

    The base prompt is yours to replace, in order of precedence: a per-request
    persona (the console's assistants send their `system` here),
    AGENT_LINUX_AGENTBOX_SYSTEM_PROMPT, a `<workspace>/.agentbox-system-prompt`
    file, or the built-in default — first match wins, so an operator can pin a
    persona without touching code and a workspace can carry its own without
    redeploying.
    """
    # ---- override 0: per-request persona ------------------------------------
    base = (override or "").strip()
    if not base:
        # ---- override 1: environment ----------------------------------------
        base = (config.AGENTBOX_SYSTEM_PROMPT or "").strip()
    if not base:
        # ---- override 2: a file beside the workspace ------------------------
        try:
            override_file = config.build_root() / ".agentbox-system-prompt"
            if override_file.is_file():
                base = override_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            base = ""
    if not base:
        base = SYSTEM_PROMPT

    parts = [base]
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


@router.get("/local-engines")
async def local_engines():
    """Local AI engines answering on this machine — nothing leaves the host.

    The point of this route is one-click privacy: if Ollama or vLLM or LM
    Studio is already running, the console can offer to wire it up as a
    provider, and the user's prompts never need to visit a third party.
    """
    found = await detect_local_engines()
    return {
        "ok": True,
        "privacy_mode": bool(config.AGENTBOX_PRIVACY_MODE),
        "engines": [_public_probe(e) for e in found],
    }


@router.post("/local-engines/add")
async def add_local_engine(request: Request):
    """Add one discovered engine as a provider — with privacy enforced.

    The base_url must be a loopback address; anything else is a mistake or an
    attempt to exfiltrate through this route, so it is refused rather than
    warned about.
    """
    body = await _body(request)
    base_url = str(body.get("base_url") or "").strip()
    if not base_url:
        return JSONResponse({"error": "base_url is required", "code": "bad_request"},
                            status_code=400)
    if not is_local_endpoint(base_url):
        return JSONResponse(
            {"error": "only loopback endpoints can be added this way",
             "code": "not_local"},
            status_code=400,
        )
    engine = str(body.get("engine") or "local").strip().lower()[:40] or "local"
    model = str(body.get("model") or "").strip()[:120]
    provider = upsert_provider(
        id=f"local-{engine}" if engine != "local" else "local",
        base_url=base_url,
        api_key="",
        model=model,
        label=f"Local {engine}",
    )
    return {"ok": True, "provider": provider.public(),
            "providers": [p.public() for p in load_providers().values()]}


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

    # Privacy mode: the check lives at the boundary, so no message is even
    # assembled for an endpoint that would leave this machine.
    try:
        assert_privacy(provider.base_url)
    except RuntimeError as err:
        return JSONResponse({"error": str(err), "code": "privacy_mode_blocked"},
                            status_code=403)

    label = body.get("label") if isinstance(body.get("label"), str) else ""
    label = label or agent_label(message)
    history = body.get("history") if isinstance(body.get("history"), list) else []
    # A per-request persona (the console's assistants) outranks every other
    # prompt source: the caller asked for *this* turn to think a certain way.
    system = body.get("system") if isinstance(body.get("system"), str) else ""
    messages: list[dict[str, Any]] = [{"role": "system",
                                       "content": await _system_prompt(system)}]
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
    try:
        assert_privacy(target.base_url)
    except RuntimeError as err:
        return JSONResponse({"error": str(err), "code": "privacy_mode_blocked"},
                            status_code=403)
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


# --------------------------------------------------------------------------- #
# Images — Cherry-style paintings: text → image against one provider
# --------------------------------------------------------------------------- #

IMAGE_SIZES = ("512x512", "768x768", "1024x1024", "1024x1792", "1792x1024")


@router.post("/images/generate")
async def generate_images(request: Request):
    """One or more images from a prompt, saved into the workspace.

    Any OpenAI-compatible `/v1/images/generations` endpoint works (OpenAI,
    an OpenRouter image model behind a compat shim, a local
    Stable-Diffusion-webui bridge). The files land in the workspace so they
    ride the same storage as everything else the agent makes; the response
    carries workspace-relative paths plus whatever the endpoint returned.
    """
    if (missing := agentbox_configured_response()) is not None:
        return missing
    body = await _body(request)
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required", "code": "bad_request"},
                            status_code=400)
    pid = body.get("provider") if isinstance(body.get("provider"), str) else ""
    if (missing := _provider_error(pid)) is not None:
        return missing
    provider = resolve_provider(pid)
    try:
        assert_privacy(provider.base_url)
    except RuntimeError as err:
        return JSONResponse({"error": str(err), "code": "privacy_mode_blocked"},
                            status_code=403)

    wanted = str(body.get("model") or "").strip()[:120] or "dall-e-3"
    size = str(body.get("size") or "1024x1024").strip()
    if size not in IMAGE_SIZES:
        size = "1024x1024"
    try:
        count = max(1, min(4, int(body.get("count") or 1)))
    except (TypeError, ValueError):
        count = 1

    # An image call goes to the provider's own endpoint, not the chat one:
    # `base_url` already ends in /v1, so `/images/generations` composes onto it.
    async with _client(provider) as client:
        try:
            res = await client.post("/images/generations", json={
                "model": wanted, "prompt": prompt[:4000],
                "n": count, "size": size, "response_format": "b64_json",
            }, timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=30.0))
        except httpx.HTTPError as err:
            return JSONResponse(
                {"error": f"the image endpoint is not answering ({err.__class__.__name__}).",
                 "code": "agentbox_unreachable"},
                status_code=502)
        if res.status_code >= 400:
            return JSONResponse(
                {"error": f"image endpoint returned HTTP {res.status_code} ({res.text[:300].strip()})",
                 "code": "agentbox_error"},
                status_code=502)
        payload = res.json() if res.content else {}
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return {"ok": True, "prompt": prompt, "images": [], "note": "the endpoint returned no images"}

    import base64
    import time as _time

    from .config import build_root

    stamp = _time.strftime("%Y%m%d-%H%M%S")
    directory = build_root() / "paintings"
    directory.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    for index, item in enumerate(items[:count]):
        if not isinstance(item, dict):
            continue
        raw = None
        if item.get("b64_json"):
            try:
                raw = base64.b64decode(str(item["b64_json"]))
            except (ValueError, TypeError):
                raw = None
        elif item.get("url"):
            saved.append({"url": str(item["url"]), "kind": "url"})
            continue
        if not raw:
            continue
        target = directory / f"painting-{stamp}-{index + 1}.png"
        try:
            with open(target, "wb") as fh:
                fh.write(raw)
        except OSError as err:
            saved.append({"error": f"cannot write {target}: {err}"})
            continue
        saved.append({"path": f"paintings/{target.name}", "bytes": len(raw), "kind": "file"})
    revised = ""
    for item in items:
        if isinstance(item, dict) and item.get("revised_prompt"):
            revised = str(item["revised_prompt"])
            break
    return {"ok": True, "prompt": prompt, "model": wanted, "size": size,
            "images": saved, "revised_prompt": revised or None}


# --------------------------------------------------------------------------- #
# Uploads — an image or text file from the composer becomes a workspace path
# --------------------------------------------------------------------------- #


@router.post("/files/upload")
async def upload_file(request: Request):
    """Multipart upload → workspace path. Small files only.

    Images ride into the conversation as `file://` mention the model can see
    only through its tools (the terminal reads them back); text files the
    model can `read_file` directly. Everything lands under `uploads/` with a
    timestamped name so two uploads never collide.
    """
    import os
    import time as _time

    from .config import build_root

    try:
        form = await request.form()
    except Exception as err:                       # noqa: BLE001
        return JSONResponse({"error": f"bad multipart form ({err.__class__.__name__})",
                             "code": "bad_request"}, status_code=400)
    part = form.get("file")
    if part is None or not hasattr(part, "read"):
        return JSONResponse({"error": "file field is required", "code": "bad_request"},
                            status_code=400)
    raw = await part.read()
    if not raw:
        return JSONResponse({"error": "the file is empty", "code": "bad_request"},
                            status_code=400)
    MAX_BYTES = 10 * 1024 * 1024
    if len(raw) > MAX_BYTES:
        return JSONResponse({"error": "file is larger than 10 MB", "code": "too_large"},
                            status_code=413)
    name = (getattr(part, "filename", "") or "upload.bin").replace("/", "_").replace("\\", "_")
    name = os.path.basename(name)[:120] or "upload.bin"
    directory = build_root() / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    target = directory / f"{stamp}-{name}"
    try:
        with open(target, "wb") as fh:
            fh.write(raw)
    except OSError as err:
        return JSONResponse({"error": f"cannot write {target}: {err}", "code": "io_error"},
                            status_code=500)
    return {"ok": True,
            "path": f"uploads/{target.name}",
            "bytes": len(raw),
            "name": name}


@router.get("/files/{file_path:path}")
async def workspace_file(file_path: str):
    """Serve a file from the workspace — paintings, screenshots, exports.

    Read-only and prefix-checked: `..` cannot climb out of the workspace, and
    only files inside it are reachable. This is how the console shows generated
    images without the host having to mount anything.
    """
    import os

    from .config import build_root

    root = build_root().resolve()
    try:
        target = (root / file_path).resolve()
    except OSError:
        return JSONResponse({"error": "bad path", "code": "bad_request"}, status_code=400)
    if not (target == root or os.path.commonpath([str(root), str(target)]) == str(root)):
        return JSONResponse({"error": "path escapes the workspace", "code": "forbidden"},
                            status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "no such file", "code": "not_found"}, status_code=404)
    from fastapi.responses import FileResponse

    return FileResponse(target, headers={"Cache-Control": "private, max-age=60"})