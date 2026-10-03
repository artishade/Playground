"""Extensions — MCP servers, skills and plugins, as one HTTP surface.

Everything an operator adds to the agent goes through this router, so the
console (and curl, and a script) has exactly one place to talk to:

    GET    /agent/extensions                  what is installed, and where it lives
    GET    /agent/extensions/tools            every extra tool the agent would see

    GET    /agent/extensions/mcp              list MCP servers
    POST   /agent/extensions/mcp              add or update one
    DELETE /agent/extensions/mcp/{id}         forget one
    POST   /agent/extensions/mcp/{id}/test    connect and list its tools

    GET    /agent/extensions/skills           list skills
    POST   /agent/extensions/skills           create one from JSON
    POST   /agent/extensions/skills/upload    upload a .md or .zip bundle
    GET    /agent/extensions/skills/{name}    one skill, body included
    PATCH  /agent/extensions/skills/{name}    enable/disable
    DELETE /agent/extensions/skills/{name}    remove

    GET    /agent/extensions/plugins          list plugins
    POST   /agent/extensions/plugins          add or update one
    DELETE /agent/extensions/plugins/{name}   remove
    POST   /agent/extensions/plugins/{name}/test   run it with sample args

    GET    /agent/extensions/db               the store, and how to point it at Supabase/Neon
    POST   /agent/extensions/db/query         run one statement (postgres backends)

Two rules run through all of it: **nothing here ever echoes a secret**, and a
broken extension never breaks the agent — it is reported and skipped.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import mcp, plugins, skills, store

log = logging.getLogger("agent_linux.extensions")

router = APIRouter()

# An upload is a skill bundle or a plugin document; anything larger is a mistake.
UPLOAD_LIMIT = 10 * 1024 * 1024


def _upload(form: Any, field: str = "file") -> Any:
    """The uploaded file from a parsed form, or None.

    Duck-typed on purpose: `fastapi.UploadFile` is a *subclass* of the starlette
    one that `request.form()` actually returns, so an `isinstance` check against
    it silently rejects every real upload.
    """
    candidate = form.get(field)
    if candidate is None:
        return None
    if isinstance(candidate, str):
        return None                    # a text field, not a file
    return candidate if hasattr(candidate, "read") else None


def fail(message: str, code: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _guard(err: Exception, code: str) -> JSONResponse:
    """One translation point, so every route answers in the same shape."""
    if isinstance(err, store.StoreUnavailable):
        return fail(str(err), err.code, 503)
    if isinstance(err, store.StoreError):
        return fail(str(err), err.code, 502)
    return fail(str(err), code)


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #


@router.get("")
async def overview():
    """What is installed, on which backend, and what the agent would see."""
    mcp_status = await mcp.status()
    skill_status = await skills.status()
    plugin_status = await plugins.status()
    problems: list[dict[str, Any]] = []
    mcp_tools = await mcp.tool_definitions(problems)
    plugin_tools = await plugins.tool_definitions()
    return {
        "ok": True,
        "store": store.describe(),
        "mcp": mcp_status,
        "skills": skill_status,
        "plugins": plugin_status,
        "extra_tools": len(mcp_tools) + len(plugin_tools) + 3,   # + read_skill, files, sql
        "unavailable": problems,
    }


@router.get("/tools")
async def tools():
    """Every extra tool the agent would see, with its origin."""
    problems: list[dict[str, Any]] = []
    mcp_tools = await mcp.tool_definitions(problems)
    plugin_tools = await plugins.tool_definitions()
    out = []
    for definition in mcp_tools + plugin_tools:
        fn = definition.get("function") or {}
        origin = "plugin" if str(fn.get("name", "")).startswith(plugins.TOOL_PREFIX) else "mcp"
        out.append({
            "name": fn.get("name"),
            "origin": origin,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    return {"ok": True, "count": len(out), "tools": out, "unavailable": problems}


# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #


@router.get("/mcp")
async def list_mcp():
    servers = await mcp.load_servers()
    out = []
    for server in sorted(servers.values(), key=lambda s: s["id"]):
        tools = mcp.cached_tools(server["id"])
        out.append(mcp.public(server, tools))
    return {"ok": True, "count": len(out), "servers": out}


@router.post("/mcp")
async def add_mcp(request: Request):
    body = await _body(request)
    try:
        server = mcp.add_server_payload(body)
        await mcp.save_server(server)
    except ValueError as err:
        return fail(str(err), "bad_request")
    except Exception as err:                      # noqa: BLE001 — translated below
        return _guard(err, "mcp_error")
    mcp.forget_tools(server["id"])
    return {"ok": True, "server": mcp.public(server)}


@router.delete("/mcp/{server_id}")
async def delete_mcp(server_id: str):
    try:
        removed = await mcp.delete_server(server_id)
    except ValueError as err:
        return fail(str(err), "bad_request")
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "mcp_error")
    if not removed:
        return fail(f"no MCP server '{server_id}'", "mcp_not_found", 404)
    mcp.forget_tools(server_id)
    return {"ok": True, "removed": server_id}


@router.post("/mcp/{server_id}/test")
async def test_mcp(server_id: str):
    """Connect, handshake, list tools. The button that answers 'is it wired?'"""
    try:
        server = await mcp.resolve(server_id)
    except ValueError as err:
        return fail(str(err), "bad_request")
    if server is None:
        return fail(f"no MCP server '{server_id}'", "mcp_not_found", 404)
    result = await mcp.probe(server)
    if result.get("ok"):
        mcp.forget_tools(server_id)     # a probe refreshes the cache by definition
    return result


@router.post("/mcp/{server_id}/toggle")
async def toggle_mcp(server_id: str, request: Request):
    body = await _body(request)
    try:
        server = await mcp.resolve(server_id)
    except ValueError as err:
        return fail(str(err), "bad_request")
    if server is None:
        return fail(f"no MCP server '{server_id}'", "mcp_not_found", 404)
    if server.get("source") == "env":
        return fail("servers baked in from AGENT_LINUX_MCP_SERVERS cannot be toggled over the API",
                    "env_server", 409)
    server["enabled"] = bool(body.get("enabled", not server.get("enabled")))
    try:
        await mcp.save_server(server)
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "mcp_error")
    mcp.forget_tools(server_id)
    return {"ok": True, "server": mcp.public(server)}


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #


@router.get("/skills")
async def list_skills():
    stored = await skills.load_skills()
    return {
        "ok": True,
        "count": len(stored),
        "skills": [skills.public(s) for s in sorted(stored.values(), key=lambda s: s["name"])],
    }


@router.get("/skills/{name}")
async def get_skill(name: str):
    skill = await skills.get_skill(name)
    if skill is None:
        return fail(f"no skill '{name}'", "skill_not_found", 404)
    return {"ok": True, "skill": skills.public(skill, with_body=True)}


@router.post("/skills")
async def create_skill(request: Request):
    """Create a skill from JSON: {name, description, body, files?}."""
    body = await _body(request)
    try:
        if isinstance(body.get("content"), str):
            # A raw markdown body with frontmatter is the friendliest input.
            skill = skills.from_markdown(str(body["content"]),
                                         str(body.get("name") or ""), source="api")
        else:
            skill = skills.from_markdown(
                _as_markdown(body), str(body.get("name") or ""), source="api")
        if body.get("name"):
            skill["name"] = skills.derive_name({"name": str(body["name"])}, skill["name"])
        await skills.save_skill(skill)
    except skills.SkillError as err:
        return fail(str(err), err.code)
    except ValueError as err:
        return fail(str(err), "bad_request")
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "skill_error")
    return {"ok": True, "skill": skills.public(skill)}


def _as_markdown(body: dict[str, Any]) -> str:
    """Rebuild a SKILL.md from structured JSON, so both doors produce one format."""
    lines = ["---", f"name: {body.get('name') or ''}"]
    if body.get("description"):
        lines.append(f"description: {body['description']}")
    if body.get("when_to_use"):
        lines.append(f"when_to_use: {body['when_to_use']}")
    lines.append("---")
    lines.append("")
    lines.append(str(body.get("body") or body.get("instructions") or ""))
    return "\n".join(lines)


@router.post("/skills/upload")
async def upload_skill(request: Request):
    """Upload a `.md` skill or a `.zip` bundle. Multipart, field name `file`."""
    form = await request.form()
    upload = _upload(form)
    if upload is None:
        return fail("attach a file (field name 'file')", "bad_request")
    blob = await upload.read()
    if not blob:
        return fail("the uploaded file is empty", "bad_request")
    if len(blob) > UPLOAD_LIMIT:
        return fail("the upload is larger than 10 MB", "bad_request")

    filename = upload.filename or ""
    try:
        if filename.lower().endswith(".zip") or blob[:2] == b"PK":
            skill = skills.from_zip(blob, filename)
        else:
            skill = skills.from_markdown(blob.decode("utf-8", "replace"), filename)
        await skills.save_skill(skill)
    except skills.SkillError as err:
        return fail(str(err), err.code)
    except ValueError as err:
        return fail(str(err), "bad_request")
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "skill_error")
    return {"ok": True, "skill": skills.public(skill)}


@router.patch("/skills/{name}")
async def patch_skill(name: str, request: Request):
    body = await _body(request)
    try:
        skill = await skills.get_skill(name)
        if skill is None:
            return fail(f"no skill '{name}'", "skill_not_found", 404)
        if "enabled" in body:
            skill["enabled"] = bool(body["enabled"])
        for field in ("description", "when_to_use", "body"):
            if isinstance(body.get(field), str):
                skill[field] = body[field]
        await skills.save_skill(skill)
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "skill_error")
    return {"ok": True, "skill": skills.public(skill)}


@router.delete("/skills/{name}")
async def delete_skill(name: str):
    try:
        removed = await skills.delete_skill(name)
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "skill_error")
    if not removed:
        return fail(f"no skill '{name}'", "skill_not_found", 404)
    return {"ok": True, "removed": name}


# --------------------------------------------------------------------------- #
# Plugins
# --------------------------------------------------------------------------- #


@router.get("/plugins")
async def list_plugins():
    stored = await plugins.load_plugins()
    return {
        "ok": True,
        "count": len(stored),
        "code_allowed": plugins.allow_code(),
        "plugins": [plugins.public(p) for p in sorted(stored.values(), key=lambda p: p["name"])],
    }


@router.get("/plugins/{name}")
async def get_plugin(name: str):
    plugin = await plugins.get_plugin(name)
    if plugin is None:
        return fail(f"no plugin '{name}'", "plugin_not_found", 404)
    # Code comes back only when this host already allows it to run: echoing it
    # on a host that refuses to execute it would only be a way to exfiltrate it.
    return {"ok": True, "plugin": plugins.public(plugin, with_code=plugins.allow_code())}


@router.post("/plugins")
async def add_plugin(request: Request):
    body = await _body(request)
    try:
        plugin = plugins.from_upload(body, source="api")
        await plugins.save_plugin(plugin)
    except (plugins.PluginError, ValueError) as err:
        return fail(str(err), getattr(err, "code", "bad_request"))
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "plugin_error")
    return {"ok": True, "plugin": plugins.public(plugin)}


@router.post("/plugins/upload")
async def upload_plugin(request: Request):
    """Upload a plugin as JSON, or as a `.py` file (code hosts only)."""
    form = await request.form()
    upload = _upload(form)
    if upload is None:
        return fail("attach a file (field name 'file')", "bad_request")
    blob = await upload.read()
    if not blob or len(blob) > UPLOAD_LIMIT:
        return fail("the upload is empty or larger than 10 MB", "bad_request")
    text = blob.decode("utf-8", "replace")
    filename = upload.filename or ""
    try:
        if filename.lower().endswith(".py"):
            name = plugins.validate_name(filename[:-3])
            plugin = plugins.from_upload({
                "name": name,
                "kind": "python",
                "description": str(form.get("description") or f"{name} plugin"),
                "code": text,
                "parameters": {"type": "object", "properties": {}},
            })
        else:
            import json

            try:
                document = json.loads(text)
            except ValueError as err:
                raise plugins.PluginError(f"'{filename}' is not valid JSON") from err
            if not isinstance(document, dict):
                raise plugins.PluginError("a plugin document must be a JSON object")
            plugin = plugins.from_upload(document)
        await plugins.save_plugin(plugin)
    except (plugins.PluginError, ValueError) as err:
        return fail(str(err), getattr(err, "code", "bad_request"))
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "plugin_error")
    return {"ok": True, "plugin": plugins.public(plugin)}


@router.post("/plugins/{name}/test")
async def test_plugin(name: str, request: Request):
    """Run a plugin with the arguments you supply — the dry-run button."""
    body = await _body(request)
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    result = await plugins.call_plugin(name, args)
    return {"ok": "error" not in result, "plugin": name, "result": result}


@router.patch("/plugins/{name}")
async def patch_plugin(name: str, request: Request):
    body = await _body(request)
    try:
        plugin = await plugins.get_plugin(name)
        if plugin is None:
            return fail(f"no plugin '{name}'", "plugin_not_found", 404)
        if "enabled" in body:
            plugin["enabled"] = bool(body["enabled"])
        if isinstance(body.get("description"), str):
            plugin["description"] = body["description"]
        if isinstance(body.get("parameters"), dict):
            plugin["parameters"] = body["parameters"]
        if isinstance(body.get("code"), str):
            plugin["code"] = body["code"]
        await plugins.save_plugin(plugin)
    except (plugins.PluginError, ValueError) as err:
        return fail(str(err), getattr(err, "code", "bad_request"))
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "plugin_error")
    return {"ok": True, "plugin": plugins.public(plugin)}


@router.delete("/plugins/{name}")
async def delete_plugin(name: str):
    try:
        removed = await plugins.delete_plugin(name)
    except ValueError as err:
        return fail(str(err), "bad_request")
    except Exception as err:                      # noqa: BLE001
        return _guard(err, "plugin_error")
    if not removed:
        return fail(f"no plugin '{name}'", "plugin_not_found", 404)
    return {"ok": True, "removed": name}


# --------------------------------------------------------------------------- #
# The database — what the store is, and how to change it
# --------------------------------------------------------------------------- #


@router.get("/db")
async def db_info():
    """The store, plus the exact schema this deployment expects.

    Returns the setup SQL so an operator can paste it into Supabase's SQL
    editor (or Neon's) without going to read the source.
    """
    info = store.describe()
    try:
        backend = store.get_store()
        reachable = True
        detail = None
        if hasattr(backend, "list"):
            await backend.list("__probe__")
    except Exception as err:                      # noqa: BLE001 — this is a report
        reachable = False
        detail = f"{err.__class__.__name__}: {err}"
    return {
        "ok": True,
        "store": info,
        "reachable": reachable,
        "error": detail,
        "setup_sql": (
            f"create table if not exists {info['table']} (\n"
            "  key        text primary key,\n"
            "  value      jsonb not null default '{}'::jsonb,\n"
            "  updated_at timestamptz not null default now()\n"
            ");"
        ),
        "env": {
            "AGENT_LINUX_STORE_BACKEND": "file | supabase | postgres",
            "AGENT_LINUX_STORE_URL": "https://<project>.supabase.co  — or a postgres:// DSN",
            "AGENT_LINUX_STORE_KEY": "supabase service_role key (not needed for a DSN)",
            "AGENT_LINUX_STORE_TABLE": "nova_docs",
            "AGENT_LINUX_STORE_READONLY": "1 to make the agent's sql tool SELECT-only",
        },
        "notes": [
            "file is the default: no database, works everywhere, lives beside the workspace.",
            "supabase uses the REST API — no driver to install, but no SQL tool.",
            "postgres (Neon, Supabase, Railway) needs `pip install asyncpg` and gives the agent SQL.",
            "A redeploy wipes a container's disk; a database is how skills and MCP servers survive it.",
        ],
    }


@router.post("/db/query")
async def db_query(request: Request):
    """One statement against the configured database. Postgres backends only."""
    body = await _body(request)
    statement = body.get("sql") or body.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        return fail("'sql' is required", "bad_request")
    params = body.get("params") if isinstance(body.get("params"), list) else None
    try:
        rows = await store.get_store().sql(statement, params)
    except store.StoreUnavailable as err:
        return fail(str(err), err.code, 503)
    except store.StoreError as err:
        return fail(str(err), err.code, 400)
    except Exception as err:                      # noqa: BLE001
        return fail(f"{err.__class__.__name__}: {err}", "store_error", 400)
    return {"ok": True, "rows": rows[:500], "count": len(rows)}


# --------------------------------------------------------------------------- #
# Catalogue — what to connect, with real, working examples
# --------------------------------------------------------------------------- #


@router.get("/catalogue")
async def catalogue():
    """Known-good starting points, so nobody has to guess a URL or a shape.

    These are examples, not endorsements: each one is a server you run or a
    public endpoint you point at, and the `notes` say which.
    """
    return {
        "ok": True,
        "mcp": [
            {
                "id": "filesystem",
                "label": "Filesystem (local, stdio)",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/app/build"],
                "notes": "The reference server. Runs in the container, so paths are the container's.",
            },
            {
                "id": "fetch",
                "label": "Fetch (local, stdio)",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-fetch"],
                "notes": "Fetches URLs and converts to markdown. No API key.",
            },
            {
                "id": "github",
                "label": "GitHub (local, stdio)",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${env.GITHUB_TOKEN}"},
                "notes": "Needs a token; put it in the host environment as GITHUB_TOKEN.",
            },
            {
                "id": "postgres",
                "label": "Postgres (local, stdio)",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-postgres", "${env.DATABASE_URL}"],
                "notes": "Read-only SQL over the same database the store can use.",
            },
            {
                "id": "remote",
                "label": "Any remote Streamable-HTTP server",
                "transport": "http",
                "url": "https://your-mcp-host.example.com/mcp",
                "headers": {"authorization": "Bearer ${env.MCP_TOKEN}"},
                "notes": "The shape to copy for a hosted server. Secrets come from the environment.",
            },
        ],
        "skill_example": {
            "name": "release-notes",
            "description": "Turn a git log into user-facing release notes.",
            "when_to_use": "When the user asks for a changelog or release notes.",
            "body": (
                "1. Run `git log --oneline <last-tag>..HEAD`.\n"
                "2. Group commits into Added / Changed / Fixed.\n"
                "3. Write for users, not for the committer: no hashes, no 'refactor'.\n"
                "4. Keep it under 20 lines."
            ),
        },
        "plugin_example": {
            "name": "notify-slack",
            "description": "Post a message to a Slack webhook.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Message to post."}},
                "required": ["text"],
            },
            "request": {
                "method": "POST",
                "url": "${env.SLACK_WEBHOOK_URL}",
                "headers": {"content-type": "application/json"},
                "body": {"text": "${text}"},
            },
        },
        "plugin_python_example": (
            "def run(args):\n"
            "    # AGENT_LINUX_PLUGINS_ALLOW_CODE=1 required on the host.\n"
            "    name = args.get('name', 'world')\n"
            "    return {'output': f'hello {name}'}\n"
        ),
    }