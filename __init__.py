"""Agent_Linux's terminal — one path, hostable on its own.

Every terminal behaviour lives under this package, and nothing in the project
reaches around it: the gateway, the agent and the dashboard all import from
`terminal.*` (see `routers/admin_terminal.py`, `main.py`, `nova/agent.py`).
That is what makes the terminal separable — the whole feature is this
directory, so it can be deployed on its own machine while the rest of the app
stays connected to it.

    agent_linux/pty.py      real PTY session manager (the shells behind the tabs)
    agent_linux/sandbox.py  simulated allowlist executor for one-shot commands
    agent_linux/link.py     LocalLink (in-process) / RemoteLink (separately hosted)
    agent_linux/api.py      the HTTP contract, mounted by BOTH hosts
    agent_linux/service.py  the standalone host: `python3 -m agent_linux.service`
    agent_linux/config.py   the AGENT_LINUX_TERMINAL_* knobs that choose the link
    agent_linux/run.sh      launcher for the standalone host

Dependency direction is one-way: `terminal` may use `nova`'s pure config,
model and telemetry helpers, never the other way round. `__init__` re-exports
the link seam only — it deliberately does not import `api` (FastAPI) or
`sandbox` (SQLAlchemy), so importing the seam stays cheap on both hosts.
"""
from __future__ import annotations

from .link import (
    LinkUnavailable,
    LocalLink,
    RemoteLink,
    SessionGone,
    SessionLimitReached,
    SessionStartFailed,
    SessionWriteFailed,
    TerminalError,
    current,
    is_remote,
    pin,
    shell_hint_for,
)

__all__ = [
    "LinkUnavailable",
    "LocalLink",
    "RemoteLink",
    "SessionGone",
    "SessionLimitReached",
    "SessionStartFailed",
    "SessionWriteFailed",
    "TerminalError",
    "current",
    "is_remote",
    "pin",
    "shell_hint_for",
]