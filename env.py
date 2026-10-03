"""Environment access, with a rename that cannot silently open a root shell.

Every knob used to be `NOVA_*`. The project is Agent_Linux now, so the prefix is
`AGENT_LINUX_*`. That rename is the one change in this codebase that can hurt
someone: an operator who had `AGENT_LINUX_TERMINAL_TOKEN` set and then upgrades would
have **no token at all**, and the service would hand out root shells to anyone
who can reach the port.

So the rule is deliberately asymmetric:

    token      read from BOTH prefixes, always. A legacy token keeps working and
               logs a loud deprecation warning. There is no situation in which
               "the old token stopped being recognised" is an acceptable outcome.
    everything  new prefix wins, old prefix still works, one warning per key.
    else

`AGENT_LINUX_*` is what the documentation uses; `NOVA_*` is supported, not
advertised, and will keep working rather than be removed — a rename that breaks
running deployments is not a rename, it is an outage.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("agent_linux.env")

NEW = "AGENT_LINUX_"
OLD = "NOVA_"

# Keys whose loss is a security regression rather than a misconfiguration. These
# are always read from both prefixes, in both directions.
SECURITY_KEYS = ("TERMINAL_TOKEN",)

# The rename also shortened some suffixes: the old names spelled the service out
# (`NOVA_TERMINAL_PORT`) where the new ones do not (`AGENT_LINUX_PORT`). Without
# this map, an operator's `NOVA_TERMINAL_PORT` would be ignored and the service
# would quietly bind somewhere else — which is how you end up debugging "why is
# nothing listening" at midnight. New suffix → the legacy suffixes it answers to.
ALIASES: dict[str, tuple[str, ...]] = {
    "PORT": ("TERMINAL_PORT",),
    "HOST": ("TERMINAL_HOST",),
    "URL": ("TERMINAL_URL",),
    "PUBLIC_URL": ("TERMINAL_PUBLIC_URL",),
    "TOKEN": ("TERMINAL_TOKEN",),
    "BUILD_ROOT": ("BUILD_ROOT",),
}

_warned: set[str] = set()


def _legacy_names(name: str) -> list[str]:
    """Every legacy variable that should satisfy `name`, most specific first."""
    return [OLD + alias for alias in ALIASES.get(name, (name,))]


def get(name: str, default: str = "") -> str:
    """One setting: `AGENT_LINUX_<name>`, then any legacy spelling, then `default`."""
    value = os.environ.get(NEW + name)
    if value is not None and value != "":
        return value
    for legacy in _legacy_names(name):
        found = os.environ.get(legacy)
        if found:
            _deprecate(name, legacy)
            return found
    return default


def flag(name: str, default: bool = False) -> bool:
    """A boolean knob: 1/true/yes are true, 0/false/no are false, anything else is `default`."""
    raw = (get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def int_or(name: str, default: int) -> int:
    raw = (get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        log.warning("%s%s is not a number (%r) — using %s", NEW, name, raw, default)
        return default


def secret(name: str) -> str:
    """A credential. Never empty when a legacy value exists — see SECURITY_KEYS."""
    value = os.environ.get(NEW + name) or ""
    legacy_hits = [(alias, os.environ.get(alias) or "") for alias in _legacy_names(name)]
    legacy = next((v for _a, v in legacy_hits if v), "")
    if not value and legacy:
        _deprecate(name, next(a for a, v in legacy_hits if v), loud=True)
        return legacy
    if value and legacy and value != legacy:
        log.warning(
            "%s%s and a legacy spelling are both set and differ — using %s%s.",
            NEW, name, NEW, name,
        )
    return value


def _deprecate(name: str, legacy_name: str, loud: bool = False) -> None:
    if legacy_name in _warned:
        return
    _warned.add(legacy_name)
    message = "%s is deprecated — rename it to %s%s."
    if loud:
        log.warning("=" * 74)
        log.warning(message, legacy_name, NEW, name)
        log.warning("The old name still works, so nothing is broken. Rename it when")
        log.warning("convenient; both spellings are read forever.")
        log.warning("=" * 74)
    else:
        log.info(message, legacy_name, NEW, name)


def describe() -> dict[str, object]:
    """Which prefix this process is actually running on, for /health."""
    legacy_in_use = sorted(
        name for name in os.environ
        if name.startswith(OLD) and not name.startswith(NEW)
    )
    return {
        "prefix": NEW,
        "legacy_prefix": OLD,
        "aliases": {key: list(values) for key, values in ALIASES.items()},
        "legacy_keys_in_use": legacy_in_use,
    }
