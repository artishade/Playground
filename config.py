"""The terminal's configuration — self-contained by design.

This module reads the environment itself and imports **nothing** from the
NovaRouter app. That is what lets `terminal/` be copied to another machine and
run on its own:

    python3 -m terminal.service        # fastapi + uvicorn + httpx, no Nova, no DB

The rest of the app keeps its own `nova/config.py` (PORT, DATABASE_URL, engine
sidecar); the two deliberately do not share code, so a terminal host never has
to know what a gateway is.

    Variable                  Where   What it does
    NOVA_TERMINAL_PORT        host    the terminal's own HTTP port (default 3100)
    NOVA_TERMINAL_URL         app     set it to use a separately hosted terminal
    NOVA_TERMINAL_TOKEN       both    shared secret; gates real root shells
    NOVA_BUILD_ROOT           host    where shells start (default /app/build)
    NOVA_AGENTBOX_BASE_URL    host    OpenAI-compatible endpoint for Agentbox
    NOVA_AGENTBOX_API_KEY     host    its API key
    NOVA_AGENTBOX_MODEL       host    model id (default: first from /models)
    NOVA_AGENTBOX_PROVIDER_FILE host  where saved providers live (default: the
                                     workspace + /.agentbox-providers.json)
"""
from __future__ import annotations

import os
from pathlib import Path

# The terminal's own package root. In the NovaRouter checkout this is the repo
# root; when `terminal/` is deployed by itself it is the directory above it.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Where shells start when nothing else is configured. Deliberately never the
# NovaRouter source tree: agent writes, `npm create` scaffolds, venvs and
# node_modules land in the workspace, never in anyone's application files.
DEFAULT_BUILD_ROOT = Path("/app/build")

# The app's HTTP port. The terminal service must never share it: a hosted
# proxy points at one port, so a second listener there answers with the wrong
# service. 3100 gives way only if the app itself is on it.
APP_PORT = int(os.environ.get("PORT") or 3000)


def engine_runtime() -> str | None:
    """Locate a JS runtime able to host the dependency-free engine sidecar."""
    from shutil import which

    for candidate in ("bun", "node"):
        if which(candidate):
            return candidate
    return None


def build_root() -> Path:
    """The workspace shells start in (`/app/build`), created on demand."""
    raw = (os.environ.get("NOVA_BUILD_ROOT") or "").strip()
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
# NOVA_TERMINAL_PORT wins, then $PORT, then our own default. `service.py` binds
# 0.0.0.0 unless NOVA_TERMINAL_HOST says otherwise (loopback-only deploys).
def _service_port() -> int:
    for raw in (os.environ.get("NOVA_TERMINAL_PORT"), os.environ.get("PORT")):
        if raw and str(raw).strip().isdigit():
            return int(raw)
    return 3100 if APP_PORT != 3100 else 3101


TERMINAL_SERVICE_PORT = _service_port()
TERMINAL_SERVICE_HOST = (os.environ.get("NOVA_TERMINAL_HOST") or "0.0.0.0").strip()

# Public URL of this host, when it is deployed behind a proxy that rewrites
# origin (Cloudflare, Render, HF Spaces). Purely cosmetic — it is echoed in
# /health so you can tell at a glance which deployment you are looking at.
TERMINAL_PUBLIC_URL = (os.environ.get("NOVA_TERMINAL_PUBLIC_URL") or "").rstrip("/")

# --------------------------------------------------------------------------- #
# Client: how the app reaches a terminal hosted somewhere else
# --------------------------------------------------------------------------- #
TERMINAL_SERVICE_URL = os.environ.get("NOVA_TERMINAL_URL", "").rstrip("/")
# Shared secret. Set it on BOTH sides: the service hands out root shells, so an
# open one must never be reachable — an unset token only suits loopback.
TERMINAL_SERVICE_TOKEN = os.environ.get("NOVA_TERMINAL_TOKEN", "")

# --------------------------------------------------------------------------- #
# Agentbox — the AI agent that ships with the terminal
# --------------------------------------------------------------------------- #
# Any OpenAI-compatible /v1/chat/completions endpoint: the free NovaFree engine,
# OpenAI, Groq, OpenRouter, or a NovaRouter gateway's own /v1. This one is the
# provider the deployer baked in (id `env`); the user adds more at runtime with
# POST /agent/providers, which persist beside the workspace.
AGENTBOX_BASE_URL = os.environ.get("NOVA_AGENTBOX_BASE_URL", "").rstrip("/")
AGENTBOX_API_KEY = os.environ.get("NOVA_AGENTBOX_API_KEY", "")
AGENTBOX_MODEL = os.environ.get("NOVA_AGENTBOX_MODEL", "")
AGENTBOX_MAX_STEPS = max(1, min(30, int(os.environ.get("NOVA_AGENTBOX_MAX_STEPS") or 8)))
AGENTBOX_STEP_TIMEOUT = int(os.environ.get("NOVA_AGENTBOX_STEP_TIMEOUT") or 120)
# Where the saved custom providers live. Empty = <workspace>/.agentbox-providers.json
AGENTBOX_PROVIDER_FILE = os.environ.get("NOVA_AGENTBOX_PROVIDER_FILE", "")


def agentbox_configured() -> bool:
    """True when at least one provider exists — the env one or a saved one.
    The full answer comes from `terminal.agentbox.load_providers()`."""
    return bool(AGENTBOX_BASE_URL)