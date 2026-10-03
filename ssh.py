"""SSH — connect to remote hosts from the terminal, with keys that stay encrypted.

A connection here is **a real terminal tab running `ssh`**. That is the whole
design decision: `pty.py` learned to exec an arbitrary `argv` on its PTY, so an
SSH session is not a proxied subprocess with a bespoke UI — it is the actual
`ssh` client, which means host-key prompts, password prompts, `~/.ssh/config`
aliases, port forwarding, `scp` inside the session and every other thing `ssh`
does all work because nothing is reimplemented.

What this module adds on top:

    keys       generated on the host (ed25519), stored **encrypted** by
               `secrets.py`, written to disk only for the moment a connection
               needs them, then removed. Nothing plaintext is left behind.
    hosts      saved connections: user, host, port, key, and which account
               profile it belongs to.
    trust      `known_hosts` is real and persistent, with an explicit
               fingerprint-confirmation route, because a silent
               `StrictHostKeyChecking=no` is how people get MITM'd.
    probe      a non-interactive `ssh -o BatchMode=yes` check that answers
               "does this key actually work" without opening a tab.

Passwords are supported but never preferred: a saved password is written to a
0600 askpass script for the duration of the connection and deleted after. A key
is better, and the routes say so.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from . import env, secrets
from .store import valid_key

log = logging.getLogger("agent_linux.ssh")

PREFIX = "ssh/"
KEY_KIND = "ssh_key"
HOST_KIND = "ssh_host"

# Where the working copies of keys and config live. Deliberately beside the
# workspace (so it survives with it) and 0700 (so only this process reads it).
SSH_DIRNAME = ".agent_linux-ssh"
DEFAULT_TIMEOUT = 15.0
PROBE_TIMEOUT = 25.0

HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class SshError(RuntimeError):
    code = "ssh_error"


# --------------------------------------------------------------------------- #
# Filesystem layout
# --------------------------------------------------------------------------- #


def ssh_dir() -> Path:
    from .config import build_root

    path = build_root() / SSH_DIRNAME
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, stat.S_IRWXU)                # 0700
    except OSError as err:
        raise SshError(f"cannot prepare {path}: {err}") from err
    return path


def known_hosts_path() -> Path:
    return ssh_dir() / "known_hosts"


def _ssh_available() -> bool:
    return bool(shutil.which("ssh"))


def _keygen_available() -> bool:
    return bool(shutil.which("ssh-keygen"))


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


def validate_name(name: str, what: str = "name") -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError(f"{what} must be lowercase letters, digits, . _ - (max 64)")
    return name


def _normalise_host(raw: str) -> str:
    host = (raw or "").strip()
    host = re.sub(r"^ssh://", "", host)
    if "@" in host:                                # user@host:port
        host = host.split("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if "/" in host:
        host = host.split("/", 1)[0]
    if not HOSTNAME_RE.match(host):
        raise ValueError(f"'{raw}' is not a usable hostname")
    return host


def generate_key(name: str, comment: str = "", key_type: str = "ed25519") -> dict[str, Any]:
    """Create a keypair on the host. Returns the document to store (encrypted)."""
    if not _keygen_available():
        raise SshError("ssh-keygen is not installed on this host")
    name = validate_name(name, "key name")
    if key_type not in ("ed25519", "rsa"):
        raise ValueError("key_type must be ed25519 or rsa")

    target = ssh_dir() / f"gen-{uuid.uuid4().hex}"
    argv = ["ssh-keygen", "-t", key_type, "-f", str(target), "-N", "", "-q",
            "-C", (comment or f"agent-linux:{name}")[:200]]
    if key_type == "rsa":
        argv += ["-b", "4096"]
    try:
        result = asyncio_run_sync(argv, timeout=60)
    except SshError:
        raise
    try:
        private = target.read_text(encoding="utf-8")
        public = (target.with_suffix(target.suffix + ".pub")).read_text(encoding="utf-8").strip()
    except OSError as err:
        raise SshError(f"ssh-keygen produced no key: {err}") from err
    finally:
        # The generated pair only ever existed to be read; the vault is the
        # copy that matters, and a stray private key on disk is exactly what we
        # are trying to avoid.
        _shred(target)
        _shred(target.with_suffix(target.suffix + ".pub"))

    return {
        "name": name,
        "type": key_type,
        "comment": comment or f"agent-linux:{name}",
        "public_key": public,
        "private_key": private,
        "fingerprint": fingerprint_of(public),
        "created_at": time.time(),
    }


def fingerprint_of(public_key: str) -> str:
    """`SHA256:…` for a public key, via ssh-keygen -lf (stdin), or a fallback."""
    if _keygen_available():
        try:
            import subprocess

            proc = subprocess.run(["ssh-keygen", "-lf", "-"], input=public_key,
                                  capture_output=True, text=True, timeout=15)
            if proc.returncode == 0 and proc.stdout.strip():
                parts = proc.stdout.split()
                if len(parts) >= 2:
                    return parts[1]
        except Exception:                          # noqa: BLE001 — fallback below
            pass
    import base64
    import hashlib

    try:
        blob = base64.b64decode(public_key.split()[1])
        digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        return f"SHA256:{digest}"
    except Exception:                              # noqa: BLE001
        return ""


def import_key(name: str, private_key: str, public_key: str = "") -> dict[str, Any]:
    """Adopt an existing private key (pasted or uploaded)."""
    name = validate_name(name, "key name")
    private = (private_key or "").strip() + "\n"
    if "PRIVATE KEY" not in private:
        raise ValueError("that does not look like a private key (no 'PRIVATE KEY' header)")
    public = (public_key or "").strip()
    if not public:
        # Derive it, so the console can always show what to paste on the server.
        if _keygen_available():
            tmp = ssh_dir() / f"derive-{uuid.uuid4().hex}"
            try:
                _write_private(tmp, private)
                import subprocess

                proc = subprocess.run(["ssh-keygen", "-y", "-f", str(tmp)],
                                      capture_output=True, text=True, timeout=20)
                if proc.returncode == 0:
                    public = proc.stdout.strip()
            except Exception:                      # noqa: BLE001
                pass
            finally:
                _shred(tmp)
    return {
        "name": name,
        "type": "ed25519" if "ED25519" in private else ("rsa" if "RSA" in private else "unknown"),
        "comment": "",
        "public_key": public,
        "private_key": private,
        "fingerprint": fingerprint_of(public) if public else "",
        "created_at": time.time(),
    }


def _write_private(path: Path, content: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content if content.endswith("\n") else content + "\n")


def _shred(path: Path) -> None:
    """Delete a private key, overwriting first where the filesystem allows it."""
    try:
        if not path.exists():
            return
        size = path.stat().st_size
        with open(path, "r+b") as fh:
            fh.write(os.urandom(max(1, size)))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


async def save_key(doc: dict[str, Any]) -> dict[str, Any]:
    payload = {**doc, "kind": KEY_KIND}
    await secrets.save(KEY_KIND, doc["name"], payload)
    return payload


async def list_keys() -> list[dict[str, Any]]:
    docs = await secrets.load_all(KEY_KIND, reveal=False)
    return sorted(docs, key=lambda d: d.get("name", ""))


async def get_key(name: str, reveal: bool = False) -> dict[str, Any] | None:
    return await secrets.load(KEY_KIND, validate_name(name, "key name"), reveal=reveal)


async def delete_key(name: str) -> bool:
    return await secrets.delete(KEY_KIND, validate_name(name, "key name"))


async def save_host(doc: dict[str, Any]) -> dict[str, Any]:
    # The key and the document's `name` are the same for hosts, so this is a
    # straight pass-through — the explicit `name` keeps it obvious that they are
    # separate concepts, because for accounts they are not the same thing.
    payload = {**doc, "kind": HOST_KIND, "name": doc.get("name")}
    await secrets.save(HOST_KIND, doc["name"], payload)
    return payload


async def list_hosts() -> list[dict[str, Any]]:
    docs = await secrets.load_all(HOST_KIND, reveal=False)
    return sorted(docs, key=lambda d: d.get("name", ""))


async def get_host(name: str, reveal: bool = False) -> dict[str, Any] | None:
    return await secrets.load(HOST_KIND, validate_name(name, "host name"), reveal=reveal)


async def delete_host(name: str) -> bool:
    return await secrets.delete(HOST_KIND, validate_name(name, "host name"))


def host_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise a connection document."""
    name = validate_name(str(body.get("name") or ""), "host name")
    hostname = _normalise_host(str(body.get("hostname") or body.get("host") or ""))
    user = str(body.get("user") or "root").strip()
    if not USER_RE.match(user):
        raise ValueError("user may contain letters, digits, . _ @ -")
    try:
        port = int(body.get("port") or 22)
    except (TypeError, ValueError):
        raise ValueError("port must be a number") from None
    if not (1 <= port <= 65535):
        raise ValueError("port must be between 1 and 65535")

    auth = str(body.get("auth") or ("key" if body.get("key") else "agent")).lower()
    if auth not in ("key", "password", "agent"):
        raise ValueError("auth must be key, password or agent")

    return {
        "name": name,
        "label": str(body.get("label") or name)[:80],
        "hostname": hostname,
        "user": user,
        "port": port,
        "auth": auth,
        "key": str(body.get("key") or "").strip().lower(),
        "password": str(body.get("password") or ""),
        "profile": str(body.get("profile") or "").strip().lower(),
        "notes": str(body.get("notes") or "")[:400],
        "added_at": time.time(),
    }


# --------------------------------------------------------------------------- #
# Running ssh
# --------------------------------------------------------------------------- #


def asyncio_run_sync(argv: list[str], timeout: float = 60) -> str:
    """Run a command to completion, in a thread-safe way (no event loop needed)."""
    import subprocess

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as err:
        raise SshError(f"{argv[0]} is not installed on this host") from err
    except subprocess.TimeoutExpired as err:
        raise SshError(f"{argv[0]} timed out after {timeout:.0f}s") from err
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise SshError(f"{argv[0]} failed: {detail}")
    return proc.stdout


def _base_options(host: dict[str, Any], key_path: Path | None,
                  batch: bool = False) -> list[str]:
    """The options every connection shares.

    `StrictHostKeyChecking=accept-new` is the honest middle ground: an unknown
    host is accepted and *recorded* the first time, and a *changed* key is
    refused loudly. `no` would be silent MITM; `yes` would make a fresh host
    unusable without a manual step the console cannot perform.
    """
    options = [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts_path()}",
        "-o", "IdentitiesOnly=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=15",
    ]
    if batch:
        options += ["-o", "BatchMode=yes"]
    if key_path is not None:
        options += ["-i", str(key_path)]
    return options


def connection_argv(host: dict[str, Any], key_path: Path | None = None,
                    extra: list[str] | None = None, remote_command: str = "") -> list[str]:
    """The argv for an ssh client.

    Order matters and is easy to get wrong: options, then `-p <port>`, then the
    **destination**, then any remote command. Putting a command before the
    destination makes ssh read the command's first word as the hostname
    ("could not resolve hostname echo"), which is a confusing way to fail.
    """
    if not _ssh_available():
        raise SshError("ssh is not installed on this host")
    argv = ["ssh", *_base_options(host, key_path), "-p", str(host["port"])]
    if extra:
        argv += extra
    argv.append(f"{host['user']}@{host['hostname']}")
    if remote_command:
        # A single string is handed to the remote shell as-is; ssh joins the
        # remaining words itself, so there is no local quoting to get wrong.
        argv.append(remote_command)
    return argv


async def probe(name: str) -> dict[str, Any]:
    """Does this connection actually work? Non-interactive, no tab opened."""
    host = await get_host(name, reveal=True)
    if host is None:
        raise SshError(f"no host '{name}'")
    started = time.time()
    cleanup: list[Path] = []
    try:
        key_path = await _materialise_key(host, cleanup)
        argv = connection_argv(host, key_path, ["-o", "BatchMode=yes"], "echo agent-linux-ok")
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_ssh_env(host, cleanup),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "host": name,
                    "error": f"no answer within {PROBE_TIMEOUT:.0f}s",
                    "latency_ms": int((time.time() - started) * 1000)}
    finally:
        for path in cleanup:
            _shred(path)

    stdout = out.decode(errors="replace").strip()
    stderr = err.decode(errors="replace").strip()
    ok = proc.returncode == 0 and "agent-linux-ok" in stdout
    result: dict[str, Any] = {
        "ok": ok,
        "host": name,
        "latency_ms": int((time.time() - started) * 1000),
        "output": stdout[:400],
    }
    if not ok:
        result["error"] = _explain(stderr or stdout)
    return result


def _explain(stderr: str) -> str:
    """Turn ssh's stderr into something actionable rather than a wall of text."""
    text = (stderr or "").strip()
    lowered = text.lower()
    if "permission denied" in lowered:
        return ("authentication failed — the key is not authorised on the server. "
                "Copy the public key into ~/.ssh/authorized_keys there. " + text[:200])
    if "host key verification failed" in lowered:
        return ("the server's host key does not match the one on record. If the host "
                "was rebuilt, forget it and reconnect; otherwise stop and check. " + text[:200])
    if "connection refused" in lowered:
        return f"nothing is listening on that port. {text[:200]}"
    if "timed out" in lowered or "timeout" in lowered:
        return f"the host did not answer in time. {text[:200]}"
    if "no such identity" in lowered:
        return f"the private key file is unusable. {text[:200]}"
    return text[:400] or "ssh failed for an unknown reason"


def _ssh_env(host: dict[str, Any], cleanup: list[Path]) -> dict[str, str]:
    """The environment an ssh client needs, including an askpass if it has a password."""
    environment = dict(os.environ)
    environment["SSH_ASKPASS_REQUIRE"] = "force"
    environment.pop("SSH_AUTH_SOCK", None)         # never borrow a host agent by accident
    password = host.get("password") or ""
    if password:
        script = ssh_dir() / f"askpass-{uuid.uuid4().hex}.sh"
        fd = os.open(str(script), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRWXU)                 # 0700: it holds a password
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # The password never appears in an argv or a process listing — it is
            # read from a file the script owns and deletes itself afterwards.
            fh.write(f"#!/bin/sh\ncat <<'AGENT_LINUX_PW'\n{password}\nAGENT_LINUX_PW\n")
        cleanup.append(script)
        environment["SSH_ASKPASS"] = str(script)
        environment["DISPLAY"] = environment.get("DISPLAY") or ":0"
    return environment


async def _materialise_key(host: dict[str, Any], cleanup: list[Path]) -> Path | None:
    """Write the host's key to a 0600 file for this connection, and track it."""
    key_name = host.get("key") or ""
    if host.get("auth") != "key" or not key_name:
        return None
    doc = await get_key(key_name, reveal=True)
    if doc is None:
        raise SshError(f"host '{host['name']}' references key '{key_name}', which no longer exists")
    private = doc.get("private_key") or ""
    if not private:
        raise SshError(f"key '{key_name}' has no private half stored")
    path = ssh_dir() / f"id-{uuid.uuid4().hex}"
    _write_private(path, private)
    cleanup.append(path)
    return path


async def open_session(host_name: str, label: str = ""):
    """Open a real ssh session as a terminal tab. Returns the session.

    The tab owns the connection: `ssh` is the process on the PTY, so the user can
    answer a prompt, run `scp`, forward a port or use an interactive program on
    the far side. The key file is removed when the session ends.
    """
    from .pty import manager

    host = await get_host(host_name, reveal=True)
    if host is None:
        raise SshError(f"no host '{host_name}'")

    cleanup: list[Path] = []
    try:
        key_path = await _materialise_key(host, cleanup)
        argv = connection_argv(host, key_path)
        environment = _ssh_env(host, cleanup)
    except Exception:
        for path in cleanup:
            _shred(path)
        raise

    try:
        session = manager.create(
            label=label or f"ssh:{host['name']}",
            argv=argv,
            env={k: v for k, v in environment.items()
                 if k in ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "DISPLAY")},
        )
    except Exception:
        for path in cleanup:
            _shred(path)
        raise

    # The temporary key and askpass live exactly as long as the tab does. A
    # watcher thread removes them the moment ssh exits, so a closed tab does not
    # leave a private key on disk.
    import threading

    def _reap() -> None:
        try:
            session.reader.join(timeout=24 * 3600)
        except Exception:                          # noqa: BLE001
            pass
        for path in cleanup:
            _shred(path)

    threading.Thread(target=_reap, daemon=True, name=f"ssh-reap-{host['name']}").start()
    return session, host


def public_host(host: dict[str, Any]) -> dict[str, Any]:
    """A host document safe to return: no password, no key material."""
    return {
        "name": host.get("name"),
        "label": host.get("label") or host.get("name"),
        "hostname": host.get("hostname"),
        "user": host.get("user"),
        "port": host.get("port"),
        "auth": host.get("auth"),
        "key": host.get("key") or None,
        "profile": host.get("profile") or None,
        "notes": host.get("notes") or "",
        "added_at": host.get("added_at"),
        "has_password": bool(host.get("password")),
        "password": secrets.mask(host.get("password", "")),
        "target": f"{host.get('user')}@{host.get('hostname')}:{host.get('port')}",
    }


def public_key(doc: dict[str, Any], with_private: bool = False) -> dict[str, Any]:
    out = {
        "name": doc.get("name"),
        "type": doc.get("type"),
        "comment": doc.get("comment") or "",
        "public_key": doc.get("public_key") or "",
        "fingerprint": doc.get("fingerprint") or "",
        "created_at": doc.get("created_at"),
        "has_private": bool(doc.get("private_key")),
    }
    if with_private:
        out["private_key"] = doc.get("private_key") or ""
    return out


async def status() -> dict[str, Any]:
    keys = await list_keys()
    hosts = await list_hosts()
    return {
        "available": _ssh_available(),
        "keygen": _keygen_available(),
        "keys": len(keys),
        "hosts": len(hosts),
        "key_names": sorted(k.get("name", "") for k in keys),
        "host_names": sorted(h.get("name", "") for h in hosts),
        "known_hosts": str(known_hosts_path()),
    }


async def known_hosts(limit: int = 200) -> list[dict[str, str]]:
    path = known_hosts_path()
    out: list[dict[str, str]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 3:
                out.append({"hosts": parts[0], "type": parts[1], "key": parts[2][:60] + "…"})
            if len(out) >= limit:
                break
    except FileNotFoundError:
        return []
    except OSError as err:
        raise SshError(f"cannot read known_hosts: {err}") from err
    return out


def forget_host_key(hostname: str) -> int:
    """Remove a host from known_hosts — what you do after a rebuild, deliberately."""
    path = known_hosts_path()
    target = _normalise_host(hostname)
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError as err:
        raise SshError(f"cannot read known_hosts: {err}") from err
    kept, removed = [], 0
    for line in lines:
        first = line.split()[0] if line.split() else ""
        if first == target or first.startswith(f"[{target}]:"):
            removed += 1
            continue
        kept.append(line)
    if removed:
        try:
            path.write_text("".join(kept), encoding="utf-8")
        except OSError as err:
            raise SshError(f"cannot write known_hosts: {err}") from err
    return removed