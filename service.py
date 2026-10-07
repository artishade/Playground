"""Agent_Linux terminal service — the Root@Build terminal, hosted on its own.

The interactive terminal is the one part of Agent_Linux that wants its own
machine: it hands out real root shells, so it can be sized, restarted and
network-isolated independently of the gateway. This module is that separate
host, and it is the only entrypoint you need if you deploy `agent_linux/` alone:

    python3 -m agent_linux.service        # binds 0.0.0.0:$AGENT_LINUX_TERMINAL_PORT
    sh ./agent_linux/run.sh               # same thing, with dependency install

It serves exactly the routes the dashboard already calls (the same
`agent_linux/api.py` the gateway mounts), plus `/agent/*` for Agentbox, so
pointing the app at it changes nothing about the UI or the API contract:

    AGENT_LINUX_TERMINAL_URL=http://terminal-host:3100   # on the Agent_Linux side
    AGENT_LINUX_TERMINAL_TOKEN=<shared secret>           # on BOTH sides

AGENT_LINUX_TERMINAL_URL unset (the default) means the gateway keeps the terminal
in-process and this module is simply not run. See `agent_linux/link.py` for
the seam, and `agent-ctx/project-map.md` for the operational notes.

This host is self-contained: it imports nothing from `nova/`, needs no
database, and ships its own Agentbox (`agent_linux/agentbox.py`) so a separately
hosted terminal still comes with the AI agent.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from agent_linux import link as terminal_link
from agent_linux.agentbox import agentbox_status
from agent_linux.agentbox import router as agentbox_router
from agent_linux.api import router as terminal_api_router
from agent_linux.config import (
    PACKAGE_ROOT,
    TERMINAL_PUBLIC_URL,
    TERMINAL_SERVICE_HOST,
    TERMINAL_SERVICE_PORT,
    TERMINAL_SERVICE_TOKEN,
    build_root,
)
from agent_linux.extensions import router as extensions_router
from agent_linux.browser_api import router as browser_router
from agent_linux.agent_x_api import router as agent_x_router
from agent_linux.credentials import router as credentials_router
from agent_linux.env import describe as env_describe
from agent_linux.store import describe as store_describe
from agent_linux import vault_backup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agent_linux.service")

STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # This process always owns its shells: pin the local link so a stray
    # AGENT_LINUX_TERMINAL_URL in the environment can't make the service proxy to
    # itself (or to a second copy of itself).
    terminal_link.pin(terminal_link.LocalLink())
    # Resolve the active database account before the first request, so a
    # deployment that has one is already using it when the console connects.
    try:
        from agent_linux import accounts as accounts_mod
        from agent_linux import store as store_mod

        config = await accounts_mod.store_config()
        store_mod.set_active_account(config)
        store_mod.reset_store()
        if config:
            log.info("store: using the active %s account", config.get("provider", config.get("backend")))
    except Exception as err:                       # noqa: BLE001 — never block boot
        log.debug("active account warm-up skipped: %s", err)
    try:
        await terminal_link.current().ensure_default()
    except Exception as err:  # never block boot on the warmup shell
        log.warning("default cloud shell warmup skipped: %s", err)
    # A redeploy reimages an ephemeral host and takes the saved providers, MCP
    # servers, skills, plugins, accounts and SSH material with it. The store is
    # the one thing that can outlive that — merge whatever it has back in.
    try:
        summary = await vault_backup.restore_if_needed()
        if summary.get("restored"):
            log.info("config restored from the %s store: %s",
                     summary.get("counts"), summary.get("items"))
    except Exception as err:                       # noqa: BLE001 — never block boot
        log.debug("config restore skipped: %s", err)
    log.info("Agent_Linux terminal service up on %s:%s (root of %s)",
             TERMINAL_SERVICE_HOST, TERMINAL_SERVICE_PORT, PACKAGE_ROOT)
    yield
    try:
        await terminal_link.current().close_all()
    except Exception as err:
        log.warning("shell teardown reported: %s", err)
    try:
        from agent_linux.browser import SESSION

        await SESSION.stop()
    except Exception as err:                      # noqa: BLE001 — shutdown is best-effort
        log.warning("browser teardown reported: %s", err)


app = FastAPI(
    title="Agent_Linux Terminal Service",
    version="2.0.0-python",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redirect_slashes=False,
)

# The service hands out root shells, so a shared secret is the difference
# between "private" and "anyone on the network". Unset is only sane on
# loopback, and is called out loudly at boot.
TOKEN_HEADERS = ("x-nova-terminal-token", "authorization")
# Open on purpose: the health endpoints are how an orchestrator knows the host
# is alive, and the console is only static assets — it sends the token with
# every API call it makes, so opening it leaks nothing.
OPEN_PATHS = ("/health", "/api/health", "/", "/index.html", "/console",
              "/agent", "/agent/", "/static/console.js")

# After every successful config mutation, copy the host's config into the store
# — the one place a redeploy cannot reach. One middleware covers every route,
# present and future; see vault_backup.py.
vault_backup.install_persistence(app)


def _ssh_report() -> dict[str, Any]:
    """SSH availability for /health — counts only, never a key."""
    import shutil

    try:
        from agent_linux import ssh as ssh_mod

        return {
            "available": bool(shutil.which("ssh")),
            "keygen": bool(shutil.which("ssh-keygen")),
            "known_hosts": str(ssh_mod.known_hosts_path()),
        }
    except Exception as err:                      # noqa: BLE001
        return {"available": False, "error": f"{err.__class__.__name__}: {err}"}


def _vault_report() -> dict[str, Any]:
    """Whether stored credentials can be encrypted on this host."""
    try:
        from agent_linux import secrets as secrets_mod

        import asyncio

        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if running:
            import importlib.util

            return {"available": importlib.util.find_spec("cryptography") is not None,
                    "key_source": None}
        return asyncio.run(secrets_mod.status())
    except Exception as err:                      # noqa: BLE001
        return {"available": False, "error": f"{err.__class__.__name__}: {err}"}


def _browser_report() -> dict[str, Any]:
    """Browser availability for /health — never launches it as a side effect."""
    try:
        import importlib.util

        installed = importlib.util.find_spec("playwright") is not None
    except Exception:                             # noqa: BLE001
        installed = False
    from agent_linux.browser import SESSION

    running = False
    try:
        running = SESSION.alive
    except Exception:                             # noqa: BLE001
        pass
    return {"playwright": installed, "running": running, "enabled": installed}


@app.middleware("http")
async def require_token(request: Request, call_next):
    if request.url.path in OPEN_PATHS:
        return await call_next(request)
    if not TERMINAL_SERVICE_TOKEN:
        return await call_next(request)
    offered = (request.headers.get(TOKEN_HEADERS[0]) or "").strip()
    if not offered:
        auth = (request.headers.get(TOKEN_HEADERS[1]) or "").strip()
        offered = auth.removeprefix("Bearer ").strip()
    if not offered:
        # EventSource and <img> cannot send headers: the browser frame stream,
        # the workspace files and any SSE view ride the token in the query
        # string instead. Same secret, weaker channel — still not public.
        offered = (request.query_params.get("token") or "").strip()
    if offered != TERMINAL_SERVICE_TOKEN:
        return JSONResponse(
            {"error": "A valid X-Nova-Terminal-Token header is required.", "code": "unauthorized"},
            status_code=401,
        )
    return await call_next(request)


# The same contract the gateway serves under /api/admin/terminal/pty.
app.include_router(terminal_api_router, prefix="/terminal/pty", tags=["terminal"])

# The console — a terminal hosted alone still has to be usable in a browser,
# so the host serves its own page at `/` (see the route below).

# Agentbox — the AI agent that ships with the terminal. It is mounted here and
# not in agent_linux/api.py on purpose: a gateway serves its own agent already
# (/api/agent/*), so the two never compete for the same routes.
app.include_router(agentbox_router, prefix="/agent", tags=["agent"])

# Extensions — MCP servers, skills and plugins the agent can be given. Nested
# under /agent so the agent owns everything it can use, and so the console has
# one prefix to talk to.
app.include_router(extensions_router, prefix="/agent/extensions", tags=["extensions"])

# The live browser — the frame stream and the controls the user drives. Mounted
# under /agent because the agent drives the *same* routes; a browser the agent
# cannot see is a black box, and one the user cannot see is worse.
app.include_router(browser_router, prefix="/agent/browser", tags=["browser"])

# SSH and accounts — credentials. Mounted under /agent because the agent uses
# them too (an ssh_run tool, an account_env tool), not only the console.
app.include_router(credentials_router, prefix="/agent", tags=["credentials"])

# Agent X — the peer-agent mesh: talk to remote models, delegate to sub-agents,
# self online-visit, self-improvement. Mounted under /agent because the main
# agent drives it too; see agent_x.py for the engine, agent_x_api.py for the
# HTTP contract the console and peers use.
app.include_router(agent_x_router, prefix="/agent/x", tags=["agent-x"])

# The config side-car — what a redeploy must not be able to take from you.
# GET /agent/backup, POST /agent/backup/snapshot, POST /agent/backup/restore.
app.include_router(vault_backup.router, prefix="/agent", tags=["backup"])


# --------------------------------------------------------------------------- #
# The page — a terminal hosted alone still has to be *usable* in a browser:
# session tabs, a real shell, and the agent. Served from the host root, so a
# deployed terminal opens on its workspace instead of a bare 404.
# --------------------------------------------------------------------------- #

_APP_ASSETS = Path(__file__).with_name("web")


@app.get("/")
@app.get("/index.html")
@app.get("/console")
@app.get("/agent")
@app.get("/agent/")
async def console():
    return FileResponse(_APP_ASSETS / "console.html",
                        media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/static/console.js")
async def console_js():
    return FileResponse(_APP_ASSETS / "console.js",
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/health")
@app.get("/api/health")
async def health():
    terminal = terminal_link.current()
    try:
        state = await terminal.snapshot()
        sessions = len(state.get("sessions") or [])
        ok = True
    except Exception:
        state, sessions, ok = {}, 0, False
    # A forgotten browser is 400 MB of Chromium; the health poll is the only
    # clock this service has, so the reaper rides it.
    try:
        from agent_linux.browser import SESSION

        await SESSION.reap_if_idle()
    except Exception:                             # noqa: BLE001
        pass
    # Agent X — peer-agent mesh status (never fails the health poll).
    try:
        from agent_linux.agent_x import get_agent_x as _ax_get
        _ax = _ax_get()
        _ax_peers = await _ax.mesh.peers()
        agentx_report = {
            "enabled": True,
            "identity": _ax.mesh.identity.public(),
            "bridges": len([p for p in _ax_peers if p.get("role") == "bridge"]),
            "peers": len(_ax_peers),
            "lessons": len(await _ax.mesh.mind(kind="lesson")),
        }
    except Exception as err:                      # noqa: BLE001
        agentx_report = {"enabled": False, "error": f"{err.__class__.__name__}: {err}"}
    return JSONResponse({
        "ok": ok,
        "status": "healthy" if ok else "degraded",
        "service": "agent_linux",
        "runtime": "python",
        "uptime_s": int(time.time() - STARTED_AT),
        "sessions": sessions,
        "active": state.get("active"),
        "max_sessions": state.get("max_sessions"),
        "build_root": str(build_root()),
        "port": TERMINAL_SERVICE_PORT,
        "public_url": TERMINAL_PUBLIC_URL or None,
        "auth_required": bool(TERMINAL_SERVICE_TOKEN),
        "agentbox": agentbox_status(),
        "agent_x": agentx_report,
        "store": store_describe(),
        "backup": {
            "key": vault_backup.SNAPSHOT_KEY,
            "auto": True,
        },
        "browser": _browser_report(),
        "env": env_describe(),
        "ssh": _ssh_report(),
        "vault": _vault_report(),
    })


if __name__ == "__main__":
    import uvicorn

    # Only reachable as `python3 -m agent_linux.service`: the `-m` form puts the
    # repo root on sys.path, which is what the `terminal.*` imports above need.
    if not TERMINAL_SERVICE_TOKEN:
        log.warning("=" * 74)
        log.warning("AGENT_LINUX_TERMINAL_TOKEN is not set — this service grants root")
        log.warning("shells to anyone who can reach it. Set the same token on both")
        log.warning("sides before exposing it beyond loopback.")
        log.warning("=" * 74)

    # Binds 0.0.0.0 so it is reachable as its own host; PORT is never reused.
    # AGENT_LINUX_TERMINAL_HOST=127.0.0.1 makes a loopback-only deploy explicit, which
    # is the sane way to run without a token behind a local reverse proxy.
    uvicorn.run(app, host=TERMINAL_SERVICE_HOST, port=TERMINAL_SERVICE_PORT,
                log_level="info", access_log=False)
