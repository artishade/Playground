"""Secrets at rest — an encrypted vault, and where it lives.

Both the SSH keys and the account credentials have to be stored, and both are
things you would be upset to leak. Two separate problems, solved separately:

    where   The store backend (file / Supabase / Postgres) decides that. On the
            `file` backend the vault is a file on the host; on a database
            backend it is a row, so it survives a redeploy. That is `store.py`'s
            job and this module does not second-guess it.

    how     Encryption, because a database row holding a plaintext private key
            is a breach waiting for a read-only credential to leak. Fernet
            (AES-128-CBC + HMAC) with a key derived from the deployment.

The key comes from `AGENT_LINUX_SECRET_KEY` if set. If it is not, one is
generated on first use and written to `<workspace>/.agent_linux-vault.key` with
0600 — which keeps a local deployment working without ceremony, and is called
out honestly: a generated key on the same disk as the data protects against a
database dump, not against someone with the disk. Set the variable in production.

Nothing here ever returns a decrypted secret to an HTTP response. `mask()` is
what the routes use; the plaintext is only ever handed to the code that needs to
*use* it (an `ssh` process, a database driver).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import stat
from pathlib import Path
from typing import Any

from . import env
from .store import StoreError, get_store, valid_key

log = logging.getLogger("agent_linux.secrets")

PREFIX = "secret/"
# A generated key is a real file with real permissions; the name says what it is.
KEY_FILENAME = ".agent_linux-vault.key"


class SecretError(RuntimeError):
    code = "secret_error"


class VaultUnavailable(SecretError):
    """The vault cannot encrypt or decrypt on this host."""

    code = "vault_unavailable"


def _require_cryptography() -> Any:
    try:
        from cryptography.fernet import Fernet, InvalidToken  # noqa: PLC0415
    except ImportError as err:
        raise VaultUnavailable(
            "storing credentials needs the `cryptography` package: "
            "pip install cryptography"
        ) from err
    return Fernet, InvalidToken


def _key_path() -> Path:
    from .config import build_root

    return build_root() / KEY_FILENAME


def _load_key() -> bytes:
    """The Fernet key: from the environment, or generated once beside the workspace."""
    configured = env.secret("SECRET_KEY").strip()
    if configured:
        # Accept either a real Fernet key or any passphrase, derived the same way
        # every time so a human-chosen value still works.
        try:
            base64.urlsafe_b64decode(configured.encode())
            if len(configured) == 44:
                return configured.encode()
        except Exception:                          # noqa: BLE001 — it is a passphrase
            pass
        digest = hashlib.sha256(configured.encode()).digest()
        return base64.urlsafe_b64encode(digest)

    path = _key_path()
    try:
        if path.exists():
            stored = path.read_text(encoding="utf-8").strip()
            if stored:
                return stored.encode()
    except OSError as err:
        raise VaultUnavailable(f"cannot read the vault key at {path}: {err}") from err

    Fernet, _ = _require_cryptography()
    key = Fernet.generate_key()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write it 0600 from the start, not chmod after — the gap between the two
        # is exactly when the key is readable by anything else on the box.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key.decode())
    except OSError as err:
        raise VaultUnavailable(f"cannot write the vault key to {path}: {err}") from err

    log.warning("=" * 74)
    log.warning("No AGENT_LINUX_SECRET_KEY is set, so a vault key was generated at:")
    log.warning("  %s", path)
    log.warning("It is 0600 and encrypts stored SSH keys and credentials. It protects")
    log.warning("against a database dump, NOT against someone who has this disk.")
    log.warning("For a real deployment set AGENT_LINUX_SECRET_KEY to a long random value")
    log.warning("and keep it in your platform's secret store instead.")
    log.warning("=" * 74)
    return key


def encrypt(plaintext: str) -> str:
    Fernet, _ = _require_cryptography()
    return Fernet(_load_key()).encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    Fernet, InvalidToken = _require_cryptography()
    try:
        return Fernet(_load_key()).decrypt(token.encode()).decode()
    except InvalidToken as err:
        raise SecretError(
            "this secret cannot be decrypted with the current vault key — it was "
            "written by a different deployment, or AGENT_LINUX_SECRET_KEY changed"
        ) from err


def mask(value: str, keep: int = 3) -> str:
    """`sk-live-abcd…` → `sk-…bcd`. Enough to recognise, useless to reuse."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "•" * len(text)
    return f"{text[:keep]}…{text[-keep:]}"


# --------------------------------------------------------------------------- #
# Documents in the store, with their secret fields encrypted
# --------------------------------------------------------------------------- #

# Field names whose value is encrypted before the document is written. Chosen by
# suffix so a new field named `…_token` is protected without a code change.
SECRET_SUFFIXES = ("_secret", "_token", "_password", "_passphrase", "_key",
                   "_private", "_credential", "_dsn", "_url")


def _is_secret_field(name: str) -> bool:
    lowered = name.lower()
    if lowered in ("url", "api_url", "base_url", "host", "endpoint", "project_url"):
        # A URL is usually not a secret, and encrypting it would make the console
        # useless for no gain. A *connection* string is, and those end in `_dsn`.
        return False
    return any(lowered.endswith(suffix) for suffix in SECRET_SUFFIXES)


def _seal(doc: dict[str, Any]) -> dict[str, Any]:
    """Encrypt every secret field, leaving a marker so we know it happened."""
    sealed = dict(doc)
    encrypted: list[str] = []
    for key, value in list(sealed.items()):
        if _is_secret_field(key) and isinstance(value, str) and value:
            sealed[key] = encrypt(value)
            encrypted.append(key)
    if encrypted:
        sealed["_encrypted"] = sorted(encrypted)
    return sealed


def _unseal(doc: dict[str, Any], reveal: bool) -> dict[str, Any]:
    """Decrypt in place, or replace with a mask — never both by accident."""
    out = dict(doc)
    fields = out.pop("_encrypted", None) or []
    for key in fields:
        value = out.get(key)
        if not isinstance(value, str) or not value:
            continue
        if reveal:
            try:
                out[key] = decrypt(value)
            except SecretError:
                out[key] = ""
                out[key + "_error"] = "cannot decrypt with the current vault key"
        else:
            out[key] = mask(_safe_plaintext(value))
    return out


def _safe_plaintext(token: str) -> str:
    """Best-effort plaintext for masking, without ever raising."""
    try:
        return decrypt(token)
    except Exception:                              # noqa: BLE001
        return ""


def doc_key(kind: str, name: str) -> str:
    return f"{PREFIX}{valid_key(kind)}/{valid_key(name)}"


async def save(kind: str, name: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Store a document, encrypting its secret fields.

    `name` here is the **storage key**, which for accounts is namespaced
    (`cloudflare--work`) and is not the same thing as the document's own `name`
    field. Clobbering the latter would rename the account on every save — and
    because the caller then builds the next key from that name, it corrupts the
    key too and the write lands somewhere else entirely. So: keep the document's
    name when it has one, and only fall back to the key.
    """
    payload = _seal({**doc, "kind": kind, "name": doc.get("name") or name})
    await get_store().put(doc_key(kind, name), payload)
    return payload


async def load(kind: str, name: str, reveal: bool = False) -> dict[str, Any] | None:
    """Read one document. `reveal=True` decrypts — for using, not for showing."""
    raw = await get_store().get(doc_key(kind, name))
    if raw is None:
        return None
    return _unseal(raw, reveal)


async def load_all(kind: str, reveal: bool = False) -> list[dict[str, Any]]:
    try:
        docs = await get_store().list(f"{PREFIX}{kind}/")
    except StoreError as err:
        log.warning("secrets: store unavailable (%s)", err)
        return []
    out = []
    for doc in docs:
        if doc.get("kind") not in (None, kind):
            continue
        out.append(_unseal(doc, reveal))
    return out


async def delete(kind: str, name: str) -> bool:
    return await get_store().delete(doc_key(kind, name))


async def status() -> dict[str, Any]:
    """For /health — whether the vault can actually work on this host."""
    try:
        _require_cryptography()
        available = True
        detail = None
    except VaultUnavailable as err:
        available = False
        detail = str(err)
    configured = bool(env.secret("SECRET_KEY").strip())
    return {
        "available": available,
        "key_source": "environment" if configured else "generated-file",
        "key_path": None if configured else str(_key_path()),
        "error": detail,
    }