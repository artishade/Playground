"""The config side-car — what a redeploy must not be able to take from you.

A `git push` reimages an ephemeral host: the container layer is rebuilt, and
everything that lived beside the workspace — saved model providers (with their
API keys), MCP servers, skills, plugins, accounts, SSH keys and hosts — is
gone with it. The store backend is the one place that can outlive that: `file`
survives on any box with a real disk (a compose volume, a VPS), and
`postgres`/`supabase` survive even a full reimage of the host itself.

So this module does three small things, forever:

    snapshot   a rolling copy of that config lives in the store, sealed with
               the same Fernet vault as every other secret (`secrets._seal`) —
               API keys and account tokens never sit in the store as plaintext,
               whether that store is a 0600 file or a row in Postgres.

    freshness  every mutating config request (provider add, MCP connect,
               skill upload, account save, …) schedules a debounced snapshot a
               moment later. One middleware, `install_persistence()`, covers
               all of the routes; no route had to learn about it.

    restore    on boot, `restore_if_needed()` merges the snapshot back in.
               Merge, never clobber: an item that already exists on the host is
               left alone, so a host that came back with newer local state
               keeps it and only the genuinely lost pieces are re-created.

The env-baked pieces (the `env` provider, `AGENT_LINUX_MCP_SERVERS`) are
deliberately NOT snapshotted — they come from the environment and survive a
redeploy by definition. Snapshotting them would let a stale copy shadow a
freshly edited env var.

Nothing here logs a key, a DSN, or a document's contents.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import accounts as accounts_mod
from . import agentbox
from . import mcp as mcp_mod
from . import plugins as plugins_mod
from . import secrets
from . import skills as skills_mod
from . import ssh as ssh_mod
from . import store

log = logging.getLogger("agent_linux.vault_backup")

# One key, one document: the whole config is small (a handful of KB), and one
# read on boot beats a fan-out of per-kind reads.
SNAPSHOT_KEY = "backup/console-config"
VERSION = 1

# The routes whose *success* means "the config changed". Matched as a prefix, so
# `/agent/extensions/skills/upload` and `/agent/accounts/x/y/activate` are both
# covered without listing every leaf.
_CONFIG_PREFIXES = (
    "/agent/providers",
    "/agent/extensions/mcp",
    "/agent/extensions/skills",
    "/agent/extensions/plugins",
    "/agent/accounts",
    "/agent/ssh/keys",
    "/agent/ssh/hosts",
)
_MUTATIONS = ("POST", "PUT", "PATCH", "DELETE")

router = APIRouter()


# --------------------------------------------------------------------------- #
# Collect — the host's live config, sealed for the store
# --------------------------------------------------------------------------- #


async def collect() -> dict[str, Any]:
    """Everything worth keeping, from the live host.

    Each section is gathered inside its own guard: one broken subsystem (a
    missing ssh binary, a store hiccup) must not cost the other sections their
    snapshot. Secrets are sealed per document — the same `_seal` the vault uses —
    so the snapshot at rest is no wider open than the store it lives in.
    """
    snap: dict[str, Any] = {}

    # Providers — the saved ones only; the env one is the deployer's business.
    try:
        providers = []
        for provider in agentbox.load_providers().values():
            if provider.source == "env":
                continue
            providers.append(secrets._seal({
                "id": provider.id,
                "label": provider.label,
                "base_url": provider.base_url,
                "api_key": provider.api_key,
                "model": provider.model,
            }))
        snap["providers"] = providers
    except Exception as err:                       # noqa: BLE001
        log.debug("snapshot: providers skipped (%s)", err)

    # MCP servers — env-baked ones are the environment's job, not ours.
    try:
        servers = []
        for server in (await mcp_mod.load_servers()).values():
            if server.get("source") == "env":
                continue
            servers.append(secrets._seal(dict(server)))
        snap["mcp"] = servers
    except Exception as err:                       # noqa: BLE001
        log.debug("snapshot: mcp skipped (%s)", err)

    try:
        snap["skills"] = [dict(skill) for skill in (await skills_mod.load_skills()).values()]
    except Exception as err:                       # noqa: BLE001
        log.debug("snapshot: skills skipped (%s)", err)

    try:
        snap["plugins"] = [dict(plugin) for plugin in (await plugins_mod.load_plugins()).values()]
    except Exception as err:                       # noqa: BLE001
        log.debug("snapshot: plugins skipped (%s)", err)

    # Accounts — reloaded with secrets revealed, then re-flattened and sealed.
    # Saving a *masked* document would encrypt the mask (the exact corruption
    # `accounts.set_active` warns about), so the snapshot only ever sees real
    # values, sealed.
    try:
        docs = []
        for doc in await accounts_mod.load_all(reveal=True):
            docs.append(secrets._seal(accounts_mod._flatten_for_vault(doc)))
        snap["accounts"] = docs
    except Exception as err:                       # noqa: BLE001
        log.debug("snapshot: accounts skipped (%s)", err)

    try:
        keys, hosts = [], []
        for key in await ssh_mod.list_keys():
            full = await ssh_mod.get_key(str(key.get("name") or ""), reveal=True)
            if full:
                keys.append(secrets._seal(full))
        for host in await ssh_mod.list_hosts():
            full = await ssh_mod.get_host(str(host.get("name") or ""), reveal=True)
            if full:
                hosts.append(secrets._seal(full))
        snap["ssh_keys"] = keys
        snap["ssh_hosts"] = hosts
    except Exception as err:                       # noqa: BLE001
        log.debug("snapshot: ssh skipped (%s)", err)

    return snap


def _counts(snap: Any) -> dict[str, int]:
    if not isinstance(snap, dict):
        return {}
    return {
        "providers": len(snap.get("providers") or []),
        "mcp": len(snap.get("mcp") or []),
        "skills": len(snap.get("skills") or []),
        "plugins": len(snap.get("plugins") or []),
        "accounts": len(snap.get("accounts") or []),
        "ssh": len(snap.get("ssh_keys") or []) + len(snap.get("ssh_hosts") or []),
    }


# --------------------------------------------------------------------------- #
# Snapshot / restore
# --------------------------------------------------------------------------- #


async def snapshot() -> dict[str, Any]:
    """Write the current config to the store. Raises StoreError on failure."""
    snap = await collect()
    snap["version"] = VERSION
    snap["saved_at"] = time.time()
    await store.get_store().put(SNAPSHOT_KEY, snap)
    return snap


async def restore() -> dict[str, Any]:
    """Merge the snapshot into the live host. Additive only, and idempotent.

    Every item is checked against what the host already has; only the missing
    ones are written. Run it ten times and the ninth does nothing — which is
    what makes it safe to call on every boot.
    """
    snap = await store.get_store().get(SNAPSHOT_KEY)
    if not isinstance(snap, dict):
        return {"restored": False, "reason": "no snapshot in the store"}
    restored: dict[str, int] = {}

    # Providers: the registry file is the truth on this host. Only when it has
    # no saved providers at all — the freshly-rebuilt-host signature — do we
    # rewrite it, so an operator's newer edits are never rolled back.
    try:
        existing = agentbox.load_providers()
        if not any(p.source == "saved" for p in existing.values()):
            providers = []
            for entry in snap.get("providers") or []:
                try:
                    doc = secrets._unseal(entry, reveal=True)
                except Exception:                  # noqa: BLE001
                    continue
                pid = str(doc.get("id") or "").strip().lower()
                base = str(doc.get("base_url") or "").strip().rstrip("/")
                if not pid or not base.lower().startswith(("http://", "https://")):
                    continue
                providers.append(agentbox.Provider(
                    id=pid, base_url=base,
                    api_key=str(doc.get("api_key") or ""),
                    model=str(doc.get("model") or "")[:120],
                    label=str(doc.get("label") or "")[:40],
                    source="saved",
                ))
            if providers:
                agentbox.save_providers(providers)
                restored["providers"] = len(providers)
    except Exception as err:                       # noqa: BLE001
        log.debug("restore: providers skipped (%s)", err)

    # MCP servers — merge by id.
    try:
        live = await mcp_mod.load_servers()
        added = 0
        for entry in snap.get("mcp") or []:
            try:
                doc = secrets._unseal(entry, reveal=True)
                server_id = mcp_mod.validate_id(str(doc.get("id") or ""))
            except Exception:                      # noqa: BLE001
                continue
            if server_id in live:
                continue
            doc["source"] = "saved"
            try:
                await mcp_mod.save_server(mcp_mod._normalise(doc))
                added += 1
            except Exception:                      # noqa: BLE001
                continue
        if added:
            restored["mcp"] = added
    except Exception as err:                       # noqa: BLE001
        log.debug("restore: mcp skipped (%s)", err)

    try:
        live_skills = await skills_mod.load_skills()
        added = 0
        for entry in snap.get("skills") or []:
            name = str(entry.get("name") or "").strip().lower()
            if not name or name in live_skills:
                continue
            try:
                await skills_mod.save_skill(dict(entry))
                added += 1
            except Exception:                      # noqa: BLE001
                continue
        if added:
            restored["skills"] = added
    except Exception as err:                       # noqa: BLE001
        log.debug("restore: skills skipped (%s)", err)

    try:
        live_plugins = await plugins_mod.load_plugins()
        added = 0
        for entry in snap.get("plugins") or []:
            name = str(entry.get("name") or "").strip().lower()
            if not name or name in live_plugins:
                continue
            try:
                await plugins_mod.save_plugin(dict(entry))
                added += 1
            except Exception:                      # noqa: BLE001
                continue
        if added:
            restored["plugins"] = added
    except Exception as err:                       # noqa: BLE001
        log.debug("restore: plugins skipped (%s)", err)

    # Accounts — unflatten first, then save (which re-flattens and re-seals).
    try:
        live_accounts = {(d.get("provider"), d.get("name"))
                         for d in await accounts_mod.load_all()}
        added = 0
        for entry in snap.get("accounts") or []:
            try:
                doc = accounts_mod._unflatten_from_vault(secrets._unseal(entry, reveal=True))
                provider = str(doc.get("provider") or "").strip().lower()
                name = str(doc.get("name") or "").strip().lower()
                if not provider or not name or (provider, name) in live_accounts:
                    continue
                await accounts_mod.save(doc)
                if doc.get("active"):
                    await accounts_mod.set_active(provider, name, True)
                added += 1
            except Exception:                      # noqa: BLE001
                continue
        if added:
            restored["accounts"] = added
    except Exception as err:                       # noqa: BLE001
        log.debug("restore: accounts skipped (%s)", err)

    # SSH keys and hosts — same merge-by-name rule.
    try:
        live_keys = {k.get("name") for k in await ssh_mod.list_keys()}
        added = 0
        for entry in snap.get("ssh_keys") or []:
            try:
                doc = secrets._unseal(entry, reveal=True)
                name = str(doc.get("name") or "")
                if not name or name in live_keys:
                    continue
                await ssh_mod.save_key(doc)
                added += 1
            except Exception:                      # noqa: BLE001
                continue
        live_hosts = {h.get("name") for h in await ssh_mod.list_hosts()}
        for entry in snap.get("ssh_hosts") or []:
            try:
                doc = secrets._unseal(entry, reveal=True)
                name = str(doc.get("name") or "")
                if not name or name in live_hosts:
                    continue
                await ssh_mod.save_host(doc)
                added += 1
            except Exception:                      # noqa: BLE001
                continue
        if added:
            restored["ssh"] = added
    except Exception as err:                       # noqa: BLE001
        log.debug("restore: ssh skipped (%s)", err)

    return {"restored": bool(restored), "items": restored, "counts": _counts(snap)}


async def restore_if_needed() -> dict[str, Any]:
    """The boot-time wrapper: never raises, never blocks startup."""
    try:
        return await restore()
    except store.StoreError as err:
        log.debug("restore skipped (%s)", err)
        return {"restored": False, "reason": str(err)}


async def status() -> dict[str, Any]:
    """What the snapshot holds and when it was taken — for the console card."""
    from . import env

    try:
        backend = store.get_store().kind
        snap = await store.get_store().get(SNAPSHOT_KEY)
        reachable = True
    except store.StoreError as err:
        return {"ok": False, "error": str(err), "backend": "unknown"}
    saved_at = snap.get("saved_at") if isinstance(snap, dict) else None
    return {
        "ok": True,
        "backend": backend,
        "reachable": reachable,
        "saved_at": saved_at,
        "age_s": int(time.time() - saved_at) if isinstance(saved_at, (int, float)) else None,
        "version": snap.get("version") if isinstance(snap, dict) else None,
        "counts": _counts(snap),
        # The vault key seals this snapshot. When it comes from the environment
        # it survives a reimage; when it is a generated file beside the
        # workspace, a rebuild replaces it and sealed secrets become
        # undecryptable — say so before it bites.
        "vault_env_key": bool(env.secret("SECRET_KEY").strip()),
    }


# --------------------------------------------------------------------------- #
# Freshness — one middleware, every config route
# --------------------------------------------------------------------------- #

_DEBOUNCE_S = 1.2
_task_lock = threading.Lock()
_pending_task: asyncio.Task | None = None


def mark_dirty() -> None:
    """Schedule a debounced snapshot. Fire-and-forget; safe from any context.

    A single save can touch two routes (the console chains them), so writes are
    coalesced: while a snapshot is pending, further marks are no-ops and the
    running task picks up whatever the host looks like when it fires.
    """
    global _pending_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _task_lock:
        if _pending_task is not None and not _pending_task.done():
            return
        _pending_task = loop.create_task(_delayed_snapshot())


async def _delayed_snapshot() -> None:
    await asyncio.sleep(_DEBOUNCE_S)
    try:
        await snapshot()
        log.info("config snapshot saved to the %s store", store.get_store().kind)
    except store.StoreError as err:
        log.warning("config snapshot failed: %s", err)
    except Exception as err:                       # noqa: BLE001
        log.warning("config snapshot failed unexpectedly: %s", err)


def install_persistence(app: Any) -> None:
    """Snapshot after every successful config mutation, on this host.

    Deliberately a middleware and not per-route calls: the config surface keeps
    growing (a new route here, an upload endpoint there) and the one thing a
    persistence guarantee must never depend on is a developer remembering to
    call `mark_dirty()`.
    """

    @app.middleware("http")
    async def _snapshot_after_config_change(request: Request, call_next):  # noqa: ANN202
        response = await call_next(request)
        try:
            if request.method in _MUTATIONS and 200 <= response.status_code < 400:
                path = request.url.path
                if "/test" not in path and any(
                    path == prefix or path.startswith(prefix + "/")
                    for prefix in _CONFIG_PREFIXES
                ):
                    mark_dirty()
        except Exception:                          # noqa: BLE001
            pass
        return response


# --------------------------------------------------------------------------- #
# Routes — the manual override
# --------------------------------------------------------------------------- #


@router.get("/backup")
async def backup_status():
    """The snapshot's shape, for the console's PERSISTENCE card."""
    return await status()


@router.post("/backup/snapshot")
async def backup_now():
    """Force a snapshot now, regardless of the debounce."""
    try:
        snap = await snapshot()
    except store.StoreError as err:
        return JSONResponse({"error": str(err), "code": err.code}, status_code=503)
    return {"ok": True, "backend": store.get_store().kind,
            "saved_at": snap.get("saved_at"), "counts": _counts(snap)}


@router.post("/backup/restore")
async def backup_restore():
    """Merge the stored snapshot into this host. Missing items only."""
    summary = await restore_if_needed()
    if summary.get("reason") == "no snapshot in the store":
        return JSONResponse({"error": "no snapshot has been saved yet",
                             "code": "no_snapshot"}, status_code=404)
    return {"ok": True, **summary}
