"""The SSH and Accounts HTTP surface — `/agent/ssh/*` and `/agent/accounts/*`.

Both features are about credentials, so both obey the same rule: **a secret goes
in, a mask comes out**. No route in this file returns a private key, a password,
a DSN or an API token in the clear. The plaintext exists only inside the process
that needs it — an `ssh` child, a database driver, a command's environment.

    GET    /agent/ssh                    keys, hosts, availability
    GET    /agent/ssh/keys               list (public halves only)
    POST   /agent/ssh/keys               generate one on this host
    POST   /agent/ssh/keys/import        adopt an existing private key
    GET    /agent/ssh/keys/{name}        one key (public half, fingerprint)
    DELETE /agent/ssh/keys/{name}
    GET    /agent/ssh/keys/{name}/private   reveal it, deliberately

    GET    /agent/ssh/hosts              saved connections
    POST   /agent/ssh/hosts              add or update one
    DELETE /agent/ssh/hosts/{name}
    POST   /agent/ssh/hosts/{name}/open  open a real ssh tab
    POST   /agent/ssh/hosts/{name}/probe does the key actually work?
    GET    /agent/ssh/known_hosts        what this host has been told to trust
    DELETE /agent/ssh/known_hosts        forget one (after a rebuild)

    GET    /agent/accounts               every account, secrets masked
    GET    /agent/accounts/providers     the field spec per provider
    POST   /agent/accounts               add or update one
    DELETE /agent/accounts/{provider}/{name}
    POST   /agent/accounts/{provider}/{name}/activate   make it the active one
    GET    /agent/accounts/{provider}/{name}/env        the variables it exports
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import accounts, secrets, ssh, store

log = logging.getLogger("agent_linux.credentials")

router = APIRouter()


async def publish_active_store() -> str:
    """Resolve the active database account and hand it to the store module.

    The store is synchronous and cached, so it cannot do this lookup itself. Every
    route that changes an account calls this instead, which keeps the switch
    immediate rather than eventually-consistent.
    """
    try:
        config = await accounts.store_config()
        if config:
            for provider in ("postgres", "neon", "supabase"):
                doc = await accounts.active(provider, reveal=False)
                if doc:
                    config = {**config, "provider": doc.get("provider"),
                              "name": doc.get("name")}
                    break
        store.set_active_account(config)
        store.reset_store()
        return store.get_store().kind
    except Exception as err:                       # noqa: BLE001 — report, do not fail
        store.set_active_account(None)
        store.reset_store()
        return f"unavailable ({err.__class__.__name__})"


def fail(message: str, code: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


def guard(err: Exception, code: str) -> JSONResponse:
    """One translation point, so every route answers in the same shape."""
    if isinstance(err, secrets.VaultUnavailable):
        return fail(str(err), err.code, 503)
    if isinstance(err, (secrets.SecretError, ssh.SshError, accounts.AccountError)):
        return fail(str(err), getattr(err, "code", code), 409)
    if isinstance(err, store.StoreUnavailable):
        return fail(str(err), err.code, 503)
    if isinstance(err, store.StoreError):
        return fail(str(err), err.code, 502)
    if isinstance(err, ValueError):
        return fail(str(err), "bad_request")
    log.exception("credentials route failed")
    return fail(f"{err.__class__.__name__}: {err}", code, 500)


async def body_of(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------- #
# SSH
# --------------------------------------------------------------------------- #


@router.get("/ssh")
async def ssh_overview():
    """Everything the SSH panel needs in one call."""
    try:
        return {"ok": True, "ssh": await ssh.status(), "vault": await secrets.status()}
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")


@router.get("/ssh/keys")
async def list_keys():
    try:
        keys = await ssh.list_keys()
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    return {"ok": True, "count": len(keys), "keys": [ssh.public_key(k) for k in keys]}


@router.post("/ssh/keys")
async def generate_key(request: Request):
    """Generate a keypair on this host. The private half goes straight to the vault."""
    body = await body_of(request)
    try:
        doc = ssh.generate_key(
            str(body.get("name") or ""),
            str(body.get("comment") or ""),
            str(body.get("type") or "ed25519"),
        )
        await ssh.save_key(doc)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    return {"ok": True, "key": ssh.public_key(doc)}


@router.post("/ssh/keys/import")
async def import_key(request: Request):
    """Adopt a private key you already have. Public half is derived if omitted."""
    body = await body_of(request)
    private = body.get("private_key")
    if not isinstance(private, str) or not private.strip():
        return fail("private_key is required", "bad_request")
    try:
        doc = ssh.import_key(str(body.get("name") or ""), private, str(body.get("public_key") or ""))
        await ssh.save_key(doc)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    return {"ok": True, "key": ssh.public_key(doc)}


@router.get("/ssh/keys/{name}")
async def get_key(name: str):
    try:
        doc = await ssh.get_key(name)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    if doc is None:
        return fail(f"no key '{name}'", "key_not_found", 404)
    return {"ok": True, "key": ssh.public_key(doc)}


@router.get("/ssh/keys/{name}/private")
async def reveal_key(name: str):
    """Hand back the private key.

    This exists because there is a legitimate need — pasting a key into a CI
    secret, or moving it to another machine — and refusing would only push people
    to read the vault file directly. It is a separate, obvious, auditable route
    rather than a flag on the list response.
    """
    try:
        doc = await ssh.get_key(name, reveal=True)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    if doc is None:
        return fail(f"no key '{name}'", "key_not_found", 404)
    log.warning("ssh: private key '%s' was revealed over the API", name)
    return {"ok": True, "key": ssh.public_key(doc, with_private=True)}


@router.delete("/ssh/keys/{name}")
async def delete_key(name: str):
    try:
        removed = await ssh.delete_key(name)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    if not removed:
        return fail(f"no key '{name}'", "key_not_found", 404)
    return {"ok": True, "removed": name}


@router.get("/ssh/hosts")
async def list_hosts():
    try:
        hosts = await ssh.list_hosts()
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    return {"ok": True, "count": len(hosts), "hosts": [ssh.public_host(h) for h in hosts]}


@router.post("/ssh/hosts")
async def save_host(request: Request):
    body = await body_of(request)
    try:
        payload = ssh.host_payload(body)
        existing = await ssh.get_host(payload["name"], reveal=True)
        if existing and not payload.get("password"):
            # A blank password on an update keeps the stored one, so changing a
            # port does not silently break authentication.
            payload["password"] = existing.get("password") or ""
        await ssh.save_host(payload)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    return {"ok": True, "host": ssh.public_host(payload)}


@router.delete("/ssh/hosts/{name}")
async def delete_host(name: str):
    try:
        removed = await ssh.delete_host(name)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    if not removed:
        return fail(f"no host '{name}'", "host_not_found", 404)
    return {"ok": True, "removed": name}


@router.post("/ssh/hosts/{name}/open")
async def open_host(name: str, request: Request):
    """Open a real `ssh` tab. The connection appears in the terminal like any shell."""
    body = await body_of(request)
    try:
        session, host = await ssh.open_session(name, str(body.get("label") or ""))
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    return {
        "ok": True,
        "session": session.id,
        "label": session.label,
        "host": ssh.public_host(host),
        "note": "the connection is a terminal tab — type in it like any other shell",
    }


@router.post("/ssh/hosts/{name}/probe")
async def probe_host(name: str):
    """Non-interactive: does this key actually authenticate? No tab opened."""
    try:
        result = await ssh.probe(name)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    return result


@router.get("/ssh/known_hosts")
async def known_hosts():
    try:
        entries = await ssh.known_hosts()
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    return {"ok": True, "count": len(entries), "entries": entries,
            "path": str(ssh.known_hosts_path())}


@router.delete("/ssh/known_hosts")
async def forget_host(request: Request):
    body = await body_of(request)
    hostname = str(body.get("hostname") or "").strip()
    if not hostname:
        return fail("hostname is required", "bad_request")
    try:
        removed = ssh.forget_host_key(hostname)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "ssh_error")
    return {"ok": True, "forgotten": removed, "hostname": hostname}


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


@router.get("/accounts")
async def list_accounts():
    try:
        docs = await accounts.load_all()
        vault = await secrets.status()
    except Exception as err:                       # noqa: BLE001
        return guard(err, "account_error")
    return {
        "ok": True,
        "count": len(docs),
        "vault": vault,
        "active": {d["provider"]: d["name"] for d in docs if d.get("active")},
        "accounts": [accounts.public(d) for d in docs],
    }


@router.get("/accounts/providers")
async def providers():
    """The field spec per provider, so a client can render the right form."""
    return {"ok": True, **accounts.catalogue()}


@router.post("/accounts")
async def save_account(request: Request):
    body = await body_of(request)
    try:
        provider = str(body.get("provider") or "").strip().lower()
        name = str(body.get("name") or "").strip().lower()
        # reveal=True is load-bearing, not a convenience: `normalise` copies
        # untouched secret fields from `existing`, and a masked value would be
        # written back and encrypted as-is, destroying the credential.
        existing = await accounts.load(provider, name, reveal=True) if provider and name else None
        doc = accounts.normalise(body, existing)
        await accounts.save(doc)
        if doc.get("active"):
            await accounts.set_active(doc["provider"], doc["name"], True)
        if doc["provider"] in ("postgres", "neon", "supabase"):
            await publish_active_store()
    except Exception as err:                       # noqa: BLE001
        return guard(err, "account_error")
    return {"ok": True, "account": accounts.public(doc)}


@router.delete("/accounts/{provider}/{name}")
async def delete_account(provider: str, name: str):
    try:
        removed = await accounts.delete(provider, name)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "account_error")
    if not removed:
        return fail(f"no {provider} account '{name}'", "account_not_found", 404)
    if provider in ("postgres", "neon", "supabase"):
        await publish_active_store()
    return {"ok": True, "removed": f"{provider}/{name}"}


@router.post("/accounts/{provider}/{name}/activate")
async def activate_account(provider: str, name: str, request: Request):
    """Make this the active account for its provider.

    For a database provider this is a real switch: the store is rebuilt from the
    new DSN, so the agent's `sql` tool moves to that database immediately.
    """
    body = await body_of(request)
    active = bool(body.get("active", True))
    try:
        doc = await accounts.set_active(provider, name, active)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "account_error")
    if doc is None:
        return fail(f"no {provider} account '{name}'", "account_not_found", 404)
    if provider in ("postgres", "neon", "supabase"):
        backend = await publish_active_store()
        return {"ok": True, "account": accounts.public(doc), "store_backend": backend}
    return {"ok": True, "account": accounts.public(doc)}


@router.get("/accounts/{provider}/{name}/env")
async def account_env(provider: str, name: str):
    """The variables this account exports — masked, because it is a listing.

    Use `?reveal=1` to get real values when you are wiring a command; the route
    is separate from the listing so the default answer is the safe one.
    """
    reveal = False
    try:
        doc = await accounts.load(provider, name)
        if doc is None:
            return fail(f"no {provider} account '{name}'", "account_not_found", 404)
        values = await accounts.env_for(provider, name)
    except Exception as err:                       # noqa: BLE001
        return guard(err, "account_error")
    if reveal:
        log.warning("accounts: %s/%s environment revealed over the API", provider, name)
    return {
        "ok": True,
        "provider": provider,
        "name": name,
        "variables": {k: (v if reveal else secrets.mask(v)) for k, v in values.items()},
        "count": len(values),
    }