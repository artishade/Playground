"""Pluggable persistence — files by default, Supabase/Postgres when you want it.

The terminal's whole selling point is that `agent_linux/` runs on its own with **no
database**. That promise cannot be broken by a feature, so this module inverts
the usual design: persistence is an interface with a zero-dependency default,
and every cloud backend is opt-in.

    AGENT_LINUX_STORE_BACKEND   file | supabase | postgres   (default: file)
    AGENT_LINUX_STORE_URL       DSN or project URL
    AGENT_LINUX_STORE_KEY       service key (Supabase) / password is in the DSN
    AGENT_LINUX_STORE_TABLE     document table name (default: nova_docs)
    AGENT_LINUX_STORE_READONLY  1 → the agent's `sql` tool may only SELECT

What lives in the store: MCP servers, skills, plugins, provider overrides. They
are **documents** (a key + a JSON value), because that is the one shape every
backend here can express cheaply:

    file       <workspace>/.nova-store/<key>.json     — always available
    supabase   REST /rest/v1/<table>?key=eq.<key>     — key text, value jsonb
    postgres   a table created on first use           — key text, value jsonb

Supabase and Neon both speak Postgres, so `postgres` covers them; `supabase` is
kept separate because the REST route needs no driver at all and works from hosts
where installing `asyncpg` is not possible.

Nothing here ever logs a DSN, a key, or a row's contents.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import env

log = logging.getLogger("agent_linux.store")

# Keys are path segments on the file backend and query values on REST, so they
# are constrained once, here, and every caller inherits the guarantee.
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,159}$")
DEFAULT_TABLE = "nova_docs"


class StoreError(RuntimeError):
    """A backend failed. `.code` is the stable wire form the routes return."""

    code = "store_error"


class StoreUnavailable(StoreError):
    code = "store_unavailable"


def valid_key(key: str) -> str:
    key = (key or "").strip().lstrip("/")
    if not KEY_RE.match(key):
        raise ValueError("invalid key: use lowercase letters, digits, . _ : / - (max 160)")
    if ".." in key:
        raise ValueError("invalid key: '..' is not allowed")
    return key


class Store(Protocol):
    """The whole contract. Four methods, and only one backend must implement all."""

    kind: str

    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def put(self, key: str, doc: dict[str, Any]) -> None: ...
    async def delete(self, key: str) -> bool: ...
    async def list(self, prefix: str = "") -> list[dict[str, Any]]: ...
    async def sql(self, statement: str, params: list[Any] | None = None) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# file — the default, and the reason the terminal still needs no database
# --------------------------------------------------------------------------- #


class FileStore:
    """JSON documents under `<workspace>/.nova-store/`. No dependency, no server.

    This is what a deployer gets when they set nothing: the features work, they
    just live beside the workspace instead of in a managed database.
    """

    kind = "file"

    def __init__(self, root: Path | None = None):
        from .config import build_root

        self.root = Path(root) if root else (build_root() / ".nova-store")
        self.lock = threading.Lock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            log.warning("store: cannot create %s (%s) — writes will fail", self.root, err)

    def _path(self, key: str) -> Path:
        # `valid_key` already rejected `..`, and this is belt-and-braces: a
        # resolved path must stay inside the store root.
        path = (self.root / f"{valid_key(key)}.json").resolve()
        root = self.root.resolve()
        if root not in path.parents and path.parent != root:
            raise ValueError("key escapes the store root")
        return path

    async def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as err:
            raise StoreError(f"cannot read {key}: {err}") from err
        return doc if isinstance(doc, dict) else None

    async def put(self, key: str, doc: dict[str, Any]) -> None:
        path = self._path(key)
        with self.lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Write-then-rename: a crash mid-write cannot truncate a good doc.
                tmp = path.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except OSError as err:
                raise StoreError(f"cannot write {key}: {err}") from err

    async def delete(self, key: str) -> bool:
        try:
            self._path(key).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as err:
            raise StoreError(f"cannot delete {key}: {err}") from err

    async def list(self, prefix: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.root.exists():
            return out
        for path in sorted(self.root.rglob("*.json")):
            key = str(path.relative_to(self.root))[:-5]
            if prefix and not key.startswith(prefix):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict):
                doc.setdefault("key", key)
                out.append(doc)
        return out

    async def sql(self, statement: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        # Honest refusal beats a fake table: the file backend has no SQL.
        raise StoreUnavailable(
            "the file store has no SQL. Set AGENT_LINUX_STORE_BACKEND=postgres "
            "(Neon/Supabase/Railway) to give the agent a queryable database."
        )


# --------------------------------------------------------------------------- #
# supabase — REST, no driver, works where pip installs are not possible
# --------------------------------------------------------------------------- #


class SupabaseStore:
    """Documents as rows in a Supabase table, over PostgREST.

    Expected schema (create it once in the SQL editor):

        create table if not exists nova_docs (
          key        text primary key,
          value      jsonb not null default '{}'::jsonb,
          updated_at timestamptz not null default now()
        );
        alter table nova_docs enable row level security;   -- service key bypasses it
    """

    kind = "supabase"

    def __init__(self, url: str, key: str, table: str = DEFAULT_TABLE):
        if not url or not key:
            raise StoreUnavailable("supabase needs both a project URL and a service key")
        self.url = url.rstrip("/")
        self.key = key
        self.table = re.sub(r"[^a-zA-Z0-9_]", "", table or DEFAULT_TABLE) or DEFAULT_TABLE
        self.client = httpx.AsyncClient(
            base_url=f"{self.url}/rest/v1",
            headers={"apikey": key, "authorization": f"Bearer {key}",
                     "content-type": "application/json"},
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
        )

    async def get(self, key: str) -> dict[str, Any] | None:
        rows = await self._rows({"key": f"eq.{valid_key(key)}", "select": "key,value", "limit": "1"})
        if not rows:
            return None
        doc = rows[0].get("value")
        return doc if isinstance(doc, dict) else None

    async def put(self, key: str, doc: dict[str, Any]) -> None:
        key = valid_key(key)
        await self._call("POST", f"/{self.table}", params={"on_conflict": "key"},
                         headers={"prefer": "resolution=merge-duplicates,return=minimal"},
                         json=[{"key": key, "value": doc}])

    async def delete(self, key: str) -> bool:
        res = await self._call("DELETE", f"/{self.table}", params={"key": f"eq.{valid_key(key)}"},
                               headers={"prefer": "return=representation"})
        try:
            return bool(res.json())
        except ValueError:
            return True

    async def list(self, prefix: str = "") -> list[dict[str, Any]]:
        params: dict[str, str] = {"select": "key,value", "order": "key.asc", "limit": "500"}
        if prefix:
            params["key"] = f"like.{prefix}*"
        rows = await self._rows(params)
        out = []
        for row in rows:
            value = row.get("value")
            if isinstance(value, dict):
                value.setdefault("key", row.get("key"))
                out.append(value)
        return out

    async def sql(self, statement: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        # PostgREST is not a SQL endpoint. Say so, and name the alternative,
        # instead of pretending an RPC function exists.
        raise StoreUnavailable(
            "the supabase REST backend cannot run arbitrary SQL; point "
            "AGENT_LINUX_STORE_BACKEND=postgres at the same database (the DSN is in "
            "Project settings → Database) to give the agent SQL access."
        )

    async def _rows(self, params: dict[str, str]) -> list[dict[str, Any]]:
        res = await self._call("GET", f"/{self.table}", params=params)
        try:
            payload = res.json()
        except ValueError:
            return []
        return payload if isinstance(payload, list) else []

    async def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            res = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            raise StoreUnavailable(f"supabase unreachable ({err.__class__.__name__})") from err
        if res.status_code >= 400:
            detail = res.text[:200].strip()
            if res.status_code in (401, 403):
                raise StoreError(f"supabase refused the key (HTTP {res.status_code})")
            if res.status_code == 404:
                raise StoreError(
                    f"supabase has no table '{self.table}' — create it first "
                    "(see agent_linux/store.py for the schema)"
                )
            raise StoreError(f"supabase HTTP {res.status_code}: {detail}")
        return res


# --------------------------------------------------------------------------- #
# postgres — Neon, Supabase, Railway, RDS, or a box in the corner
# --------------------------------------------------------------------------- #


class PostgresStore:
    """Documents in Postgres, plus real SQL for the agent's `sql` tool.

    `asyncpg` is imported lazily so the package still runs with three
    dependencies when this backend is not selected:

        pip install asyncpg        # or: pip install '.[postgres]'
    """

    kind = "postgres"

    def __init__(self, dsn: str, table: str = DEFAULT_TABLE, readonly: bool = False):
        if not dsn:
            raise StoreUnavailable("postgres needs a DSN in AGENT_LINUX_STORE_URL")
        self.dsn = dsn
        self.table = re.sub(r"[^a-zA-Z0-9_]", "", table or DEFAULT_TABLE) or DEFAULT_TABLE
        self.readonly = readonly
        self._pool: Any = None
        self._lock = threading.Lock()
        self._ready = False

    def _require(self) -> Any:
        try:
            import asyncpg  # noqa: PLC0415 — deliberate: optional dependency
        except ImportError as err:
            raise StoreUnavailable(
                "the postgres backend needs asyncpg: pip install asyncpg "
                "(or use AGENT_LINUX_STORE_BACKEND=supabase, which needs no driver)"
            ) from err
        return asyncpg

    async def _connect(self) -> Any:
        asyncpg = self._require()
        if self._pool is None:
            try:
                self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4, timeout=15)
            except Exception as err:
                raise StoreUnavailable(f"postgres refused the connection: {err}") from err
        return self._pool

    async def _ensure(self) -> Any:
        pool = await self._connect()
        if not self._ready:
            with self._lock:
                self._ready = True
            async with pool.acquire() as conn:
                await conn.execute(
                    f"create table if not exists {self.table} ("
                    "  key text primary key,"
                    "  value jsonb not null default '{}'::jsonb,"
                    "  updated_at timestamptz not null default now()"
                    ")"
                )
        return pool

    async def get(self, key: str) -> dict[str, Any] | None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"select value from {self.table} where key = $1", valid_key(key))
        if row is None:
            return None
        value = row["value"]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        return value if isinstance(value, dict) else None

    async def put(self, key: str, doc: dict[str, Any]) -> None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            await conn.execute(
                f"insert into {self.table} (key, value, updated_at) values ($1, $2::jsonb, now()) "
                f"on conflict (key) do update set value = excluded.value, updated_at = now()",
                valid_key(key), json.dumps(doc),
            )

    async def delete(self, key: str) -> bool:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            res = await conn.execute(f"delete from {self.table} where key = $1", valid_key(key))
        return res.endswith(" 1")

    async def list(self, prefix: str = "") -> list[dict[str, Any]]:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            if prefix:
                rows = await conn.fetch(
                    f"select key, value from {self.table} where key like $1 order by key limit 500",
                    f"{prefix}%",
                )
            else:
                rows = await conn.fetch(f"select key, value from {self.table} order by key limit 500")
        out = []
        for row in rows:
            value = row["value"]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            if isinstance(value, dict):
                value.setdefault("key", row["key"])
                out.append(value)
        return out

    async def sql(self, statement: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """One statement, rows back. Writes are blocked when readonly."""
        text = (statement or "").strip().rstrip(";")
        if not text:
            raise StoreError("empty statement")
        if self.readonly:
            first = text.lstrip("( \n\t").split(None, 1)[0].lower()
            if first not in ("select", "with", "show", "explain", "table"):
                raise StoreError("this store is read-only (AGENT_LINUX_STORE_READONLY=1)")
        if ";" in text:
            raise StoreError("one statement per call — send them separately")
        pool = await self._ensure()
        async with pool.acquire() as conn:
            try:
                rows = await conn.fetch(text, *(params or []))
            except Exception as err:
                raise StoreError(f"{err.__class__.__name__}: {err}") from err
        out = []
        for row in rows:
            item = {}
            for column, value in dict(row).items():
                if isinstance(value, (bytes, bytearray)):
                    value = f"<{len(value)} bytes>"
                elif hasattr(value, "isoformat"):
                    value = value.isoformat()
                elif not isinstance(value, (str, int, float, bool, type(None), list, dict)):
                    value = str(value)
                item[column] = value
            out.append(item)
        return out


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #

_STORE: Store | None = None
_STORE_LOCK = threading.Lock()


def describe() -> dict[str, Any]:
    """What the store is, for /health — never the DSN, never the key."""
    backend = env.get("STORE_BACKEND").strip().lower()
    url = env.get("STORE_URL")
    if not backend:
        backend = "postgres" if url else "file"
    info: dict[str, Any] = {
        "backend": backend,
        "table": env.get("STORE_TABLE") or DEFAULT_TABLE,
        "configured": backend == "file" or bool(url),
        "readonly": env.flag("STORE_READONLY"),
    }
    if url and backend == "supabase":
        # A project URL is not a secret; the key is, and never appears here.
        info["project"] = url.split("//")[-1].split("/")[0]
    return info


def get_store() -> Store:
    """The process-wide store. Selection is configuration, so it is cached."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        backend = env.get("STORE_BACKEND").strip().lower()
        url = env.get("STORE_URL").strip()
        key = env.get("STORE_KEY").strip()
        table = env.get("STORE_TABLE") or DEFAULT_TABLE
        readonly = env.flag("STORE_READONLY")

        if not backend:
            backend = "postgres" if url else "file"

        if backend == "file":
            _STORE = FileStore()
        elif backend == "supabase":
            _STORE = SupabaseStore(url, key, table)
        elif backend in ("postgres", "postgresql", "neon"):
            _STORE = PostgresStore(url, table, readonly)
        else:
            raise StoreUnavailable(
                f"unknown AGENT_LINUX_STORE_BACKEND '{backend}' — use file, supabase or postgres"
            )
        log.info("store: %s backend selected%s", _STORE.kind, " (read-only)" if readonly else "")
        return _STORE


def reset_store() -> None:
    """Drop the cached store — tests, and a config change at runtime."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
