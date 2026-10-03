"""The terminal's configuration — self-contained by design.

This module reads the environment itself and imports **nothing** from any host
application. That is what lets `agent_linux/` be copied to another machine and
run on its own:

    python3 -m agent_linux.service      # fastapi + uvicorn + httpx, no Nova, no DB

Settings come from `AGENT_LINUX_*` (see `env.py`); the legacy `NOVA_*` names are
still read, and the token is read from both prefixes unconditionally so a rename
can never leave the service unauthenticated.

    Variable                            What it does
    AGENT_LINUX_PORT                    the service's own HTTP port (default 3100)
    AGENT_LINUX_HOST                    bind address (default 0.0.0.0)
    AGENT_LINUX_TOKEN                   shared secret; gates real root shells
    AGENT_LINUX_PUBLIC_URL              cosmetic; echoed in /health
    AGENT_LINUX_BUILD_ROOT              where shells start (default /app/build)
    AGENT_LINUX_URL                     on the app side: reach a terminal hosted elsewhere
    AGENT_LINUX_AGENTBOX_BASE_URL       OpenAI-compatible endpoint for Agentbox
    AGENT_LINUX_AGENTBOX_API_KEY        its API key
    AGENT_LINUX_AGENTBOX_MODEL          model id (default: first from /models)
    AGENT_LINUX_AGENTBOX_MAX_STEPS      tool-call budget per task (default 8)
    AGENT_LINUX_AGENTBOX_STEP_TIMEOUT   seconds one tool call may take (default 120)
    AGENT_LINUX_AGENTBOX_PROVIDER_FILE  where saved providers live
    AGENT_LINUX_STORE_BACKEND           file | supabase | postgres
    AGENT_LINUX_STORE_URL               project URL or postgres DSN
    AGENT_LINUX_STORE_KEY               supabase service key
    AGENT_LINUX_STORE_TABLE             document table (default nova_docs)
    AGENT_LINUX_STORE_READONLY          make the agent's `sql` tool SELECT-only
    AGENT_LINUX_MCP_SERVERS             JSON array of MCP servers baked in at deploy
    AGENT_LINUX_PLUGINS_ALLOW_CODE      allow python plugins (same trust as shell)
"""
from __future__ import annotations

import os
from pathlib import Path

from . import env

# The package's parent directory. In a checkout this is the repo root; when the
# package is deployed by itself it is the directory above it.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Where shells start when nothing else is configured. Deliberately never the
# source tree: agent writes, `npm create` scaffolds, venvs and node_modules land
# in the workspace, never in anyone's application files.
DEFAULT_BUILD_ROOT = Path("/app/build")

# The host application's HTTP port, when this runs as part of a bigger app. The
# service must never share it: a hosted proxy points at one port, so a second
# listener there answers with the wrong service.
APP_PORT = int((os.environ.get("PORT") or "").strip() or 3000)


def engine_runtime() -> str | None:
    """Locate a JS runtime able to host the dependency-free engine sidecar."""
    from shutil import which

    for candidate in ("bun", "node"):
        if which(candidate):
            return candidate
    return None


def build_root() -> Path:
    """The workspace shells start in (`/app/build`), created on demand."""
    raw = env.get("BUILD_ROOT").strip()
    if raw:
        root = Path(raw)
    elif os.geteuid() == 0 or os.access("/", os.W_OK):
        root = DEFAULT_BUILD_ROOT
    else:
        # Not root and no writable /app: keep the workspace beside the package
        # so a local run still works.
        root = PACKAGE_ROOT / "app_build"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return PACKAGE_ROOT / "build"
    return root


# --------------------------------------------------------------------------- #
# Host: where this process binds when it is the terminal service
# --------------------------------------------------------------------------- #
# Most free hosts inject $PORT and route their public URL at it, so an explicit
# AGENT_LINUX_PORT wins, then $PORT, then our own default.
def _service_port() -> int:
    explicit = env.get("PORT").strip()
    if explicit.isdigit():
        return int(explicit)
    injected = (os.environ.get("PORT") or "").strip()
    if injected.isdigit():
        return int(injected)
    return 3100 if APP_PORT != 3100 else 3101


TERMINAL_SERVICE_PORT = _service_port()
TERMINAL_SERVICE_HOST = (env.get("HOST") or "0.0.0.0").strip()

# Public URL of this host, when it is deployed behind a proxy that rewrites
# origin (Cloudflare, Render, HF Spaces). Purely cosmetic — it is echoed in
# /health so you can tell at a glance which deployment you are looking at.
TERMINAL_PUBLIC_URL = env.get("PUBLIC_URL").rstrip("/")

# --------------------------------------------------------------------------- #
# Client: how an app reaches a terminal hosted somewhere else
# --------------------------------------------------------------------------- #
TERMINAL_SERVICE_URL = env.get("URL").rstrip("/")
# Shared secret. Set it on BOTH sides: the service hands out root shells, so an
# open one must never be reachable — an unset token only suits loopback.
# `secret()` reads both prefixes and warns loudly, because silently losing this
# one is the difference between private and public.
TERMINAL_SERVICE_TOKEN = env.secret("TOKEN")

# --------------------------------------------------------------------------- #
# Agentbox — the AI agent that ships with the terminal
# --------------------------------------------------------------------------- #
# Any OpenAI-compatible /v1/chat/completions endpoint: OpenAI, Groq, OpenRouter,
# or a local vLLM. This one is the provider the deployer baked in (id `env`); the
# user adds more at runtime with POST /agent/providers, which persist beside the
# workspace.
AGENTBOX_BASE_URL = env.get("AGENTBOX_BASE_URL").rstrip("/")
AGENTBOX_API_KEY = env.secret("AGENTBOX_API_KEY")
AGENTBOX_MODEL = env.get("AGENTBOX_MODEL")
AGENTBOX_MAX_STEPS = max(1, min(30, env.int_or("AGENTBOX_MAX_STEPS", 8)))
AGENTBOX_STEP_TIMEOUT = env.int_or("AGENTBOX_STEP_TIMEOUT", 120)
# Where the saved custom providers live. Empty = <workspace>/.agentbox-providers.json
AGENTBOX_PROVIDER_FILE = env.get("AGENTBOX_PROVIDER_FILE")


def agentbox_configured() -> bool:
    """True when at least one provider exists — the env one or a saved one.
    The full answer comes from `agent_linux.agentbox.load_providers()`."""
    return bool(AGENTBOX_BASE_URL)