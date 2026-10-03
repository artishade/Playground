"""Plugins — extra tools for the agent, without shipping code.

Two kinds, and the difference matters:

    declarative (default)  A JSON document: a name, a description, an argument
                           schema, and a URL to call. The agent gets a new tool
                           and the host runs **no third-party code**. This is
                           the safe path and covers most "call my API" plugins.

    python (opt-in)        A file with a `run(args) -> dict` function, loaded
                           only when AGENT_LINUX_PLUGINS_ALLOW_CODE=1. Same trust level
                           as shell access, which the terminal already has —
                           but it is off unless a deployer says otherwise, so
                           an upload can never become remote code execution by
                           accident.

A declarative plugin's request is templated from the model's arguments:

    {"method": "POST", "url": "https://api.example.com/notes",
     "headers": {"authorization": "Bearer ${env.NOTES_TOKEN}"},
     "body": {"title": "${title}", "tags": "${tags}"}}

`${name}` is substituted from the tool arguments, `${env.NAME}` from the host
environment — so a secret lives in the deployment, never in the plugin document
and never in the model's context. Response bodies are truncated and returned as
text; the agent reads them like any other tool result.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import httpx

from . import env
from .store import StoreError, get_store, valid_key

log = logging.getLogger("terminal.plugins")

PREFIX = "plugin/"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
PLACEHOLDER_RE = re.compile(r"\$\{([a-zA-Z0-9_.]+)\}")
TIMEOUT = 60.0
OUTPUT_LIMIT = 12000
MAX_PLUGINS = 32

# The tool name the model sees. A single `__` prefix keeps plugin tools
# distinguishable from MCP's `mcp__` namespace at a glance.
TOOL_PREFIX = "plugin__"


class PluginError(RuntimeError):
    code = "plugin_error"


def allow_code() -> bool:
    return env.flag("PLUGINS_ALLOW_CODE")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def _doc_key(name: str) -> str:
    return f"{PREFIX}{valid_key(name)}"


def validate_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("plugin name must be lowercase letters, digits, . _ - (max 40)")
    return name


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    kind = str(raw.get("kind") or "http").strip().lower()
    if kind not in ("http", "python"):
        raise PluginError("kind must be 'http' or 'python'")
    schema = raw.get("parameters")
    if not isinstance(schema, dict) or not schema.get("type"):
        schema = {"type": "object", "properties": {}}
    request = raw.get("request") if isinstance(raw.get("request"), dict) else {}
    method = str(request.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        raise PluginError(f"unsupported method '{method}'")
    return {
        "name": str(raw.get("name") or ""),
        "label": str(raw.get("label") or raw.get("name") or ""),
        "description": str(raw.get("description") or "Custom plugin tool")[:900],
        "kind": kind,
        "enabled": bool(raw.get("enabled", True)),
        "parameters": schema,
        "request": {
            "method": method,
            "url": str(request.get("url") or "").strip(),
            "headers": {str(k): str(v) for k, v in (request.get("headers") or {}).items()},
            "body": request.get("body"),
            "timeout_s": float(request.get("timeout_s") or TIMEOUT),
        },
        "code": str(raw.get("code") or ""),
        "source": str(raw.get("source") or "upload"),
        "notes": str(raw.get("notes") or "")[:400],
        "added_at": raw.get("added_at") or time.time(),
    }


def _public(plugin: dict[str, Any], with_code: bool = False) -> dict[str, Any]:
    request = dict(plugin.get("request") or {})
    # A header value can be a literal secret. Mask anything that looks like one;
    # `${env.X}` is left readable because it names a variable, not a value.
    headers = {}
    for key, value in (request.get("headers") or {}).items():
        lowered = key.lower()
        looks_secret = any(w in lowered for w in ("key", "token", "secret", "auth", "password"))
        if looks_secret and "${env." not in str(value):
            value = f"{str(value)[:3]}…{str(value)[-3:]}" if len(str(value)) > 8 else "•" * len(str(value))
        headers[key] = value
    request["headers"] = headers
    out = {
        "name": plugin["name"],
        "label": plugin["label"] or plugin["name"],
        "description": plugin["description"],
        "kind": plugin["kind"],
        "enabled": plugin["enabled"],
        "parameters": plugin["parameters"],
        "request": request if plugin["kind"] == "http" else None,
        "has_code": bool(plugin.get("code")),
        "code_bytes": len(plugin.get("code") or ""),
        "source": plugin["source"],
        "notes": plugin["notes"],
        "added_at": plugin["added_at"],
    }
    if with_code:
        out["code"] = plugin.get("code") or ""
    return out


async def load_plugins() -> dict[str, dict[str, Any]]:
    try:
        docs = await get_store().list(PREFIX)
    except StoreError as err:
        log.warning("plugins: store unavailable (%s)", err)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for doc in docs:
        try:
            plugin = _normalise(doc)
        except (PluginError, ValueError):
            continue
        if plugin["name"]:
            out[plugin["name"]] = plugin
    return out


async def get_plugin(name: str) -> dict[str, Any] | None:
    return (await load_plugins()).get(validate_name(name))


async def save_plugin(plugin: dict[str, Any]) -> dict[str, Any]:
    name = validate_name(plugin.get("name") or "")
    if plugin.get("kind") == "http" and not plugin["request"]["url"]:
        raise PluginError("an http plugin needs request.url")
    if plugin.get("kind") == "python" and not allow_code():
        raise PluginError(
            "python plugins are disabled on this host. A declarative http plugin "
            "needs no code and works now; set AGENT_LINUX_PLUGINS_ALLOW_CODE=1 to enable "
            "python plugins (same trust as shell access)."
        )
    plugin["name"] = name
    await get_store().put(_doc_key(name), plugin)
    return plugin


async def delete_plugin(name: str) -> bool:
    return await get_store().delete(_doc_key(validate_name(name)))


async def set_enabled(name: str, enabled: bool) -> dict[str, Any] | None:
    plugin = await get_plugin(name)
    if plugin is None:
        return None
    plugin["enabled"] = bool(enabled)
    await save_plugin(plugin)
    return plugin


def from_upload(raw: dict[str, Any], source: str = "upload") -> dict[str, Any]:
    """A plugin document from an upload, with the name derived when absent."""
    body = dict(raw)
    if not body.get("name"):
        label = str(body.get("label") or body.get("title") or "").strip()
        body["name"] = re.sub(r"[^a-z0-9._-]+", "-", label.lower()).strip("-")
    body["source"] = source
    return _normalise({**body, "name": validate_name(body.get("name") or "")})


# --------------------------------------------------------------------------- #
# Templating
# --------------------------------------------------------------------------- #


def _lookup(path: str, args: dict[str, Any]) -> Any:
    """`${title}` or `${env.TOKEN}` or `${user.name}` — dotted, from either map."""
    if path.startswith("env."):
        return os.environ.get(path[4:], "")
    current: Any = args
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return ""
    return current


def render(value: Any, args: dict[str, Any]) -> Any:
    """Substitute `${…}` through strings, dicts and lists.

    A string that is *exactly* one placeholder keeps its native type, so
    `"${count}"` sends a number when the model passed a number — which is what
    an API expects, and a silent string would be a bug the model cannot see.
    """
    if isinstance(value, str):
        whole = PLACEHOLDER_RE.fullmatch(value.strip())
        if whole:
            return _lookup(whole.group(1), args)
        return PLACEHOLDER_RE.sub(lambda m: str(_lookup(m.group(1), args)), value)
    if isinstance(value, dict):
        return {k: render(v, args) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, args) for v in value]
    return value


def _missing(args: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(key) for key in required if key not in args or args.get(key) in ("", None)]


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


async def run_http(plugin: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    request = plugin["request"]
    missing = _missing(args, plugin["parameters"])
    if missing:
        return {"error": f"missing required argument(s): {', '.join(missing)}"}

    url = render(request["url"], args)
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return {"error": f"the resolved url is not http(s): {url!r}"}
    headers = {k: str(render(v, args)) for k, v in (request.get("headers") or {}).items()}
    body = render(request.get("body"), args) if request.get("body") is not None else None

    kwargs: dict[str, Any] = {"headers": headers}
    if body is not None and request["method"] not in ("GET", "HEAD"):
        kwargs["json"] = body
    elif body is not None:
        # A GET with a body is unusual; a query string is what the API means.
        kwargs["params"] = body if isinstance(body, dict) else {}

    timeout = httpx.Timeout(connect=10.0, read=min(request.get("timeout_s") or TIMEOUT, 300.0),
                            write=30.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            res = await client.request(request["method"], url, **kwargs)
    except httpx.HTTPError as err:
        return {"error": f"{plugin['name']} is unreachable ({err.__class__.__name__})"}

    text = res.text[:OUTPUT_LIMIT]
    try:
        parsed = res.json()
        if isinstance(parsed, (dict, list)):
            text = json.dumps(parsed)[:OUTPUT_LIMIT]
    except ValueError:
        pass

    out: dict[str, Any] = {
        "plugin": plugin["name"],
        "status": res.status_code,
        "output": text,
    }
    if res.status_code >= 400:
        out["error"] = f"HTTP {res.status_code}: {text[:400]}"
    return out


def run_python(plugin: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Load a plugin's `run(args)` — only ever with code execution allowed.

    The module is built in memory and never written to disk: a plugin document
    that is deleted leaves nothing behind, and nothing can import it by accident.
    """
    if not allow_code():
        return {"error": "python plugins are disabled on this host (AGENT_LINUX_PLUGINS_ALLOW_CODE)"}
    code = plugin.get("code") or ""
    if not code.strip():
        return {"error": f"plugin '{plugin['name']}' has no code"}
    namespace: dict[str, Any] = {
        "__name__": f"nova_plugin_{plugin['name']}",
        "__builtins__": __builtins__,
    }
    try:
        exec(compile(code, f"<plugin:{plugin['name']}>", "exec"), namespace)  # noqa: S102
    except Exception as err:
        return {"error": f"the plugin failed to load: {err.__class__.__name__}: {err}"}
    entry = namespace.get("run")
    if not callable(entry):
        return {"error": "the plugin defines no run(args) function"}
    try:
        result = entry(args if isinstance(args, dict) else {})
    except Exception as err:
        return {"error": f"the plugin raised {err.__class__.__name__}: {err}"}
    if isinstance(result, dict):
        return result
    return {"output": str(result)[:OUTPUT_LIMIT]}


async def call_plugin(name: str, args: dict[str, Any]) -> dict[str, Any]:
    plugin = await get_plugin(name)
    if plugin is None:
        return {"error": f"no plugin '{name}'"}
    if not plugin.get("enabled", True):
        return {"error": f"plugin '{name}' is disabled"}
    if plugin["kind"] == "python":
        return run_python(plugin, args)
    return await run_http(plugin, args)


# --------------------------------------------------------------------------- #
# What the agent sees
# --------------------------------------------------------------------------- #


def tool_definition(plugin: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": f"{TOOL_PREFIX}{plugin['name']}",
            "description": str(plugin["description"] or "Custom plugin")[:900],
            "parameters": plugin["parameters"],
        },
    }


def parse_tool_name(tool_name: str) -> str | None:
    if not tool_name.startswith(TOOL_PREFIX):
        return None
    name = tool_name[len(TOOL_PREFIX):]
    return name if NAME_RE.match(name) else None


async def tool_definitions(cap: int = MAX_PLUGINS) -> list[dict[str, Any]]:
    plugins = [p for p in (await load_plugins()).values() if p.get("enabled", True)]
    return [tool_definition(p) for p in plugins[:cap]]


async def status() -> dict[str, Any]:
    plugins = await load_plugins()
    return {
        "plugins": len(plugins),
        "enabled": sum(1 for p in plugins.values() if p.get("enabled", True)),
        "names": sorted(plugins),
        "code_allowed": allow_code(),
        "kinds": sorted({p["kind"] for p in plugins.values()}),
    }


def public(plugin: dict[str, Any], with_code: bool = False) -> dict[str, Any]:
    return _public(plugin, with_code)