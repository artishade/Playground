"""Accounts — many credentials per provider, switchable at a glance.

The problem this solves is mundane and real: you have a personal and a work
Supabase project, two Cloudflare accounts, three Google service accounts, a Neon
branch database per environment. One global `AGENT_LINUX_STORE_URL` cannot
express that, and pasting a DSN into a shell command is how credentials end up
in a history file.

So accounts are **named documents**, grouped by provider:

    provider   supabase | neon | postgres | cloudflare | google | github | generic
    name       `work`, `personal`, `staging` — what you call it
    fields     the credential set for that provider, secrets encrypted at rest

Each provider declares its own fields, so the console can render the right form
and the code can validate before saving:

    postgres     url (DSN)  ·  sslmode
    supabase     project_url, service_key, anon_key, db_url
    neon         url (DSN)  ·  branch
    cloudflare   account_id, api_token, zone_id, r2_bucket
    google       service_account_json, project_id, bucket
    github       token, owner
    generic      any key/value pair

Two things make accounts *useful* rather than just stored:

    activate()   marks one account active **per provider**, and for `postgres`,
                 `neon` and `supabase` that is not cosmetic: the active account
                 becomes what `store.get_store()` reads, so switching accounts
                 switches the database the agent's `sql` tool talks to.

    env_for()    turns an account into environment variables for a command —
                 `${env.CF_API_TOKEN}` in a plugin, or an `aws`-style CLI in a
                 shell tab. The value is injected into that process only.

A secret is never returned by the API. `public()` masks every secret field, and
the plaintext leaves this module only through `env_for()` or `active_store_config()`.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import secrets
from .store import valid_key

log = logging.getLogger("agent_linux.accounts")

PREFIX = "account/"
KIND = "account"

NAME_RE_LEN = 64


class AccountError(RuntimeError):
    code = "account_error"


# --------------------------------------------------------------------------- #
# Providers — what fields each one has, and which are secret
# --------------------------------------------------------------------------- #

# `secret=True` fields are encrypted at rest by secrets.py and masked in every
# response. `required` fields are validated before a save. `hint` is shown in
# the console so nobody has to guess where a value comes from.
PROVIDERS: dict[str, dict[str, Any]] = {
    "postgres": {
        "label": "PostgreSQL (any host)",
        "fields": [
            {"name": "url", "secret": True, "required": True,
             "hint": "postgresql://user:pass@host:5432/db"},
            {"name": "sslmode", "secret": False, "hint": "require / prefer / disable"},
        ],
        "note": "Works with Neon, Supabase, Railway, RDS or a box you own.",
    },
    "neon": {
        "label": "Neon",
        "fields": [
            {"name": "url", "secret": True, "required": True,
             "hint": "postgresql://user:pass@ep-xxx.neon.tech/db"},
            {"name": "branch", "secret": False, "hint": "main / preview / your branch name"},
        ],
        "note": "Neon is Postgres, so this is a labelled postgres account.",
    },
    "supabase": {
        "label": "Supabase",
        "fields": [
            {"name": "project_url", "secret": False, "required": True,
             "hint": "https://<project>.supabase.co"},
            {"name": "service_key", "secret": True,
             "hint": "service_role key — the REST backend uses this"},
            {"name": "anon_key", "secret": True, "hint": "anon/public key"},
            {"name": "db_url", "secret": True,
             "hint": "optional DSN, for the SQL tool instead of REST"},
        ],
        "note": "project_url + service_key drives the REST store; db_url adds SQL.",
    },
    "cloudflare": {
        "label": "Cloudflare",
        "fields": [
            {"name": "account_id", "secret": False, "hint": "dash → Workers & Pages → Account ID"},
            {"name": "api_token", "secret": True, "required": True,
             "hint": "My Profile → API Tokens → Create Token"},
            {"name": "zone_id", "secret": False, "hint": "optional, for a specific domain"},
            {"name": "r2_bucket", "secret": False, "hint": "optional R2 bucket name"},
        ],
        "note": "Used by wrangler and the Cloudflare API; exported as CF_* variables.",
    },
    "google": {
        "label": "Google Cloud",
        "fields": [
            {"name": "service_account_json", "secret": True,
             "hint": "paste the whole service-account JSON key"},
            {"name": "project_id", "secret": False, "hint": "my-project-123456"},
            {"name": "bucket", "secret": False, "hint": "optional GCS bucket"},
            {"name": "region", "secret": False, "hint": "us-central1"},
        ],
        "note": "The JSON is written to a temp file and exported as GOOGLE_APPLICATION_CREDENTIALS.",
    },
    "github": {
        "label": "GitHub",
        "fields": [
            {"name": "token", "secret": True, "required": True,
             "hint": "fine-grained PAT, or a classic token with repo scope"},
            {"name": "owner", "secret": False, "hint": "username or organisation"},
        ],
        "note": "Exported as GITHUB_TOKEN for git and the MCP GitHub server.",
    },
    "generic": {
        "label": "Generic key/value",
        "fields": [
            {"name": "values", "secret": False,
             "hint": "JSON object, e.g. {\"API_KEY\":\"…\",\"REGION\":\"eu\"}"},
        ],
        "note": "Any credential set. Values are exported verbatim into a command's environment.",
    },
}


def provider_spec(provider: str) -> dict[str, Any]:
    spec = PROVIDERS.get((provider or "").strip().lower())
    if spec is None:
        raise ValueError(
            f"unknown provider '{provider}' — use one of: {', '.join(sorted(PROVIDERS))}"
        )
    return spec


def _field_names(provider: str) -> list[str]:
    return [f["name"] for f in provider_spec(provider)["fields"]]


def _is_secret_field(provider: str, field: str) -> bool:
    for spec in provider_spec(provider)["fields"]:
        if spec["name"] == field:
            return bool(spec.get("secret"))
    return False


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


def _doc_key(provider: str, name: str) -> str:
    return f"{PREFIX}{valid_key(provider)}/{valid_key(name)}"


def normalise(body: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a request body into an account document.

    A blank secret field on an update keeps the stored value — the same rule the
    model providers use, because re-typing a token to change a label is silly.
    """
    provider = str(body.get("provider") or "").strip().lower()
    spec = provider_spec(provider)

    name = str(body.get("name") or "").strip().lower()
    name = "".join(ch for ch in name if ch.isalnum() or ch in "._-")[:NAME_RE_LEN]
    if not name:
        raise ValueError("an account needs a name, e.g. `work` or `personal`")

    fields: dict[str, str] = {}
    incoming = body.get("fields") if isinstance(body.get("fields"), dict) else {}
    # Flat keys are accepted too, so `{"api_token": "…"}` works as well as
    # `{"fields": {"api_token": "…"}}` — the console sends the flat form.
    for key in _field_names(provider):
        value = incoming.get(key, body.get(key))
        if value is None:
            continue
        fields[key] = value if isinstance(value, str) else json.dumps(value)

    if existing:
        for key in _field_names(provider):
            if not fields.get(key) and existing.get(key):
                fields[key] = existing[key]

    for field in spec["fields"]:
        if field.get("required") and not (fields.get(field["name"]) or "").strip():
            raise ValueError(f"{field['name']} is required for a {provider} account")

    doc: dict[str, Any] = {
        "provider": provider,
        "name": name,
        "label": str(body.get("label") or name)[:80],
        "notes": str(body.get("notes") or "")[:400],
        "fields": fields,
        "active": bool(body.get("active", existing.get("active") if existing else False)),
        "added_at": (existing or {}).get("added_at") or time.time(),
        "updated_at": time.time(),
    }
    return doc


def _flatten_for_vault(doc: dict[str, Any]) -> dict[str, Any]:
    """secrets.py encrypts by field-name suffix, so name the secret keys clearly.

    The *unsuffixed* copy of a secret field is dropped here, deliberately. Leaving
    it in would put a plaintext token in the stored document next to the encrypted
    one, which defeats the encryption — and on the way back out it would shadow
    the decrypted value with whatever was written last.
    """
    provider = doc["provider"]
    out = {k: v for k, v in doc.items() if k != "fields"}
    fields: dict[str, str] = {}
    for key, value in (doc.get("fields") or {}).items():
        if _is_secret_field(provider, key):
            out[f"{key}_secret"] = value          # encrypted by secrets.py
        else:
            fields[key] = value
    out["fields"] = fields
    return out


def _unflatten_from_vault(stored: dict[str, Any]) -> dict[str, Any]:
    """Move the decrypted `…_secret` fields back into `fields`.

    The order matters: secrets are taken out of the top level *first*, then the
    plain fields are merged, so a decrypted secret is never overwritten by a
    stale plaintext copy of itself.
    """
    out = dict(stored)
    provider = out.get("provider") or ""
    fields = dict(out.get("fields") or {})
    for key in list(out.keys()):
        if not key.endswith("_secret"):
            continue
        field = key[: -len("_secret")]
        out.pop(key)
        if _is_secret_field(provider, field) or field in _field_names(provider):
            fields[field] = stored[key]           # decrypted value wins
    out["fields"] = fields
    return out


async def save(doc: dict[str, Any]) -> dict[str, Any]:
    payload = _flatten_for_vault(doc)
    await secrets.save(KIND, f"{doc['provider']}--{doc['name']}", payload)
    return doc


async def load(provider: str, name: str, reveal: bool = False) -> dict[str, Any] | None:
    raw = await secrets.load(KIND, f"{valid_key(provider)}--{valid_key(name)}", reveal=reveal)
    if raw is None:
        return None
    return _unflatten_from_vault(raw)


async def load_all(reveal: bool = False) -> list[dict[str, Any]]:
    docs = await secrets.load_all(KIND, reveal=reveal)
    out = [_unflatten_from_vault(d) for d in docs]
    return sorted(out, key=lambda d: (d.get("provider", ""), d.get("name", "")))


async def delete(provider: str, name: str) -> bool:
    return await secrets.delete(KIND, f"{valid_key(provider)}--{valid_key(name)}")


async def set_active(provider: str, name: str, active: bool = True) -> dict[str, Any] | None:
    """One active account per provider; activating one deactivates its siblings.

    This reads the sibling documents with `reveal=True` and writes them back
    **unchanged**. That is not incidental: writing back a masked document would
    encrypt the mask itself, silently destroying the credential — the account
    would still look configured while its token had become `sk-…bcd`. Reading
    with `reveal=True` is what keeps a flag change from corrupting a secret.
    """
    target = await load(provider, name, reveal=True)
    if target is None:
        return None
    for doc in await load_all(reveal=True):
        if doc.get("provider") != provider:
            continue
        should_be_active = active and doc.get("name") == name
        if bool(doc.get("active")) != should_be_active:
            doc["active"] = should_be_active
            await save(doc)
    return await load(provider, name)


async def active(provider: str, reveal: bool = False) -> dict[str, Any] | None:
    for doc in await load_all(reveal=reveal):
        if doc.get("provider") == provider and doc.get("active"):
            return doc
    return None


# --------------------------------------------------------------------------- #
# Using an account
# --------------------------------------------------------------------------- #

# The variables a command gets when an account is activated. Kept close to what
# the tools themselves expect, so `wrangler`/`gcloud`/`git` need no wrapper.
ENV_MAP: dict[str, dict[str, str]] = {
    "cloudflare": {
        "account_id": "CLOUDFLARE_ACCOUNT_ID",
        "api_token": "CLOUDFLARE_API_TOKEN",
        "zone_id": "CLOUDFLARE_ZONE_ID",
        "r2_bucket": "R2_BUCKET",
    },
    "github": {"token": "GITHUB_TOKEN", "owner": "GITHUB_OWNER"},
    "google": {"project_id": "GOOGLE_CLOUD_PROJECT", "bucket": "GCS_BUCKET",
               "region": "GOOGLE_CLOUD_REGION"},
    "postgres": {"url": "DATABASE_URL", "sslmode": "PGSSLMODE"},
    "neon": {"url": "DATABASE_URL", "branch": "NEON_BRANCH"},
    "supabase": {"project_url": "SUPABASE_URL", "service_key": "SUPABASE_SERVICE_KEY",
                 "anon_key": "SUPABASE_ANON_KEY", "db_url": "DATABASE_URL"},
}


async def env_for(provider: str, name: str) -> dict[str, str]:
    """The environment variables this account contributes, decrypted.

    `generic` accounts export their JSON object verbatim; every other provider
    uses ENV_MAP. This is what a shell tab or a plugin subprocess receives — the
    secret is never written into a command line where `ps` could read it.
    """
    doc = await load(provider, name, reveal=True)
    if doc is None:
        raise AccountError(f"no {provider} account '{name}'")
    fields = doc.get("fields") or {}
    if doc.get("provider") == "generic":
        raw = fields.get("values") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as err:
            raise AccountError(f"account '{name}' has invalid JSON in `values`") from err
        if not isinstance(parsed, dict):
            raise AccountError(f"account '{name}' `values` must be a JSON object")
        return {str(k): str(v) for k, v in parsed.items()}

    mapping = ENV_MAP.get(doc.get("provider"), {})
    out: dict[str, str] = {}
    for field, variable in mapping.items():
        value = fields.get(field)
        if value:
            out[variable] = str(value)
    if doc.get("provider") == "google" and fields.get("service_account_json"):
        # gcloud and the client libraries want a *file path*, not a JSON blob.
        path = _write_credentials_file(name, str(fields["service_account_json"]))
        out["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
    return out


def _write_credentials_file(name: str, payload: str) -> Any:
    """Write a service-account JSON to a 0600 file and return its path."""
    import os
    import stat
    from pathlib import Path

    from .config import build_root

    directory = build_root() / ".agent_linux-credentials"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, stat.S_IRWXU)
    except OSError:
        pass
    path = Path(directory) / f"{valid_key(name)}.json"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return path


async def store_config() -> dict[str, str] | None:
    """The store settings implied by the active database account, if any.

    This is the part that makes accounts more than a password manager: with a
    postgres/neon/supabase account active, the store backend, DSN and key come
    from it instead of the environment, so switching accounts switches the
    database the agent talks to.
    """
    for provider in ("postgres", "neon", "supabase"):
        doc = await active(provider, reveal=True)
        if doc is None:
            continue
        fields = doc.get("fields") or {}
        if provider in ("postgres", "neon") and fields.get("url"):
            config = {"backend": "postgres", "url": fields["url"]}
            if fields.get("sslmode"):
                config["sslmode"] = fields["sslmode"]
            return config
        if provider == "supabase" and fields.get("project_url"):
            if fields.get("db_url"):
                # A DSN is strictly more capable (it adds the `sql` tool), so it
                # wins when both halves are present.
                return {"backend": "postgres", "url": fields["db_url"]}
            return {"backend": "supabase", "url": fields["project_url"],
                    "key": fields.get("service_key", "")}
    return None


def public(doc: dict[str, Any]) -> dict[str, Any]:
    """An account safe to return: every secret field masked, never dropped.

    Masking rather than hiding is deliberate — the console needs to show *that*
    a token is set so the user knows the account is complete.
    """
    provider = doc.get("provider", "")
    fields: dict[str, str] = {}
    for key, value in (doc.get("fields") or {}).items():
        if _is_secret_field(provider, key):
            fields[key] = secrets.mask(str(value))
        elif key == "service_account_json":
            fields[key] = secrets.mask(str(value), keep=8)
        else:
            fields[key] = str(value)
    return {
        "provider": provider,
        "name": doc.get("name"),
        "label": doc.get("label") or doc.get("name"),
        "notes": doc.get("notes") or "",
        "active": bool(doc.get("active")),
        "fields": fields,
        "secret_fields": [f["name"] for f in PROVIDERS.get(provider, {"fields": []})["fields"]
                          if f.get("secret")],
        "added_at": doc.get("added_at"),
        "updated_at": doc.get("updated_at"),
    }


def catalogue() -> dict[str, Any]:
    """The provider specs, so a client can render the right form."""
    return {
        "providers": [
            {
                "id": key,
                "label": spec["label"],
                "fields": spec["fields"],
                "note": spec.get("note", ""),
                "env": ENV_MAP.get(key, {}),
            }
            for key, spec in PROVIDERS.items()
        ]
    }


async def status() -> dict[str, Any]:
    docs = await load_all()
    by_provider: dict[str, int] = {}
    for doc in docs:
        by_provider[doc.get("provider", "?")] = by_provider.get(doc.get("provider", "?"), 0) + 1
    actives = {doc["provider"]: doc["name"] for doc in docs if doc.get("active")}
    return {
        "accounts": len(docs),
        "by_provider": by_provider,
        "active": actives,
        "providers": sorted(PROVIDERS),
    }