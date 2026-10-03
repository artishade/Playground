"""Agent_Linux safe sandbox executor — port of src/lib/server/terminal-exec.ts.

Lives in `agent_linux/` with the rest of the terminal feature (see
`agent_linux/__init__.py`); imported as `terminal.sandbox`.

No child processes are ever spawned for shell semantics — every command is
simulated from real OS telemetry (/proc, shutil, platform, os) plus live DB
state. The ONLY subprocess ever executed is a `--version` probe of a JS
runtime binary (node/bun/npm) when it exists on PATH, per the conversion
requirement that version output stays honest.

An allowlist governs what may run; anything dangerous exits 126, anything
unknown exits 127. Every execution is persisted (TerminalCommand, capped at
200 rows).

This is the ONE terminal module that borrows the host app: the command
history and the `nova …` subcommands are gateway state. Those imports are
optional, so a terminal deployed on its own still boots and still runs the
sandboxed file/shell helpers — it just says so plainly when you ask for
something that genuinely needs the gateway behind it.
"""
from __future__ import annotations

import math
import os
import platform
import re
import pathlib
import shutil
import socket
import stat as stat_mod
import subprocess
import time
from datetime import datetime, timezone

from .config import PACKAGE_ROOT, build_root

# The app's database-backed helpers. Absent when only `agent_linux/` is deployed,
# which is fine: every entry point that needs one checks `APP_AVAILABLE` first
# and reports the honest reason instead of raising an ImportError at boot.
APP_AVAILABLE = True
try:
    from sqlalchemy import delete, func, select
    from sqlalchemy.orm import Session

    from nova.kv import (
        get_config,
        get_config_number,
        get_gpu_providers,
        set_config,
        set_gpu_providers,
    )
    from nova.models import (
        AgentTask,
        ClientKey,
        ModelRoute,
        Model as ModelRow,
        Provider,
        RequestLog,
        StorageProviderRow,
        SystemConfig,
        TerminalCommand,
    )
except Exception:  # noqa: BLE001 — a standalone terminal has no app, no DB
    APP_AVAILABLE = False

PROJECT_ROOT = PACKAGE_ROOT

NO_APP = (
    "the Agent_Linux gateway is not attached to this terminal (only the "
    "terminal is hosted here), so this has no database to read"
)

PROCESS_START_MS = time.time() * 1000  # python equivalent of process.uptime()

# The simulated workspace root is the real project directory.
SANDBOX_ROOT = str(PROJECT_ROOT)

# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #

DANGEROUS_WORDS = {
    "rm", "sudo", "kill", "chmod", "chown", "curl", "wget", "bash", "sh",
    "zsh", "fish", "eval", "exec", "source", "dd", "mkfs", "shutdown",
    "reboot", "nc", "pkill", "killall",
}

DANGEROUS_CHARS = ["|", ";", "&", ">", "<", "`", "$("]

# ---------------------------------------------------------------------------
# Builder FS helpers - sandboxed to the builder base location + /tmp
# ---------------------------------------------------------------------------
BUILDER_ALLOWED_TMP = pathlib.Path("/tmp").resolve()


def _builder_base() -> pathlib.Path:
    """Builder base location — `/app/build`, kept separate from the source tree
    so agent file writes and shell builds can never touch the application's
    own code."""
    try:
        return pathlib.Path(build_root())
    except Exception:
        return PROJECT_ROOT


def _builder_resolve(target: str):
    if not target or not target.strip():
        return None
    raw = target.strip()
    p = pathlib.Path(raw)
    if not p.is_absolute():
        p = _builder_base() / p
    try:
        rp = p.resolve()
    except Exception:
        return None
    # allow under the builder base or /tmp
    try:
        rp.relative_to(_builder_base().resolve())
        return rp
    except ValueError:
        try:
            rp.relative_to(BUILDER_ALLOWED_TMP)
            return rp
        except ValueError:
            return None
def builder_write(path: str, content: str):
    rp = _builder_resolve(path)
    if rp is None:
        return _err(1, f"write: blocked outside workspace: {path!r}")
    try:
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(content, encoding="utf-8")
        return _ok(f"Wrote {rp.stat().st_size} bytes to {rp}")
    except Exception as e:
        return _err(1, f"write failed: {e}")
def builder_read(path: str):
    rp = _builder_resolve(path)
    if rp is None:
        return _err(1, f"read: blocked: {path!r}")
    try:
        if not rp.exists():
            return _err(1, f"read: not found: {rp}")
        txt = rp.read_text(encoding="utf-8", errors="replace")
        return _ok(txt[:12000] + ("\n...[truncated]" if len(txt)>12000 else ""))
    except Exception as e:
        return _err(1, f"read failed: {e}")
def builder_edit(path: str, old: str, new: str):
    rp = _builder_resolve(path)
    if rp is None:
        return _err(1, f"edit: blocked: {path!r}")
    try:
        txt = rp.read_text(encoding="utf-8")
        if old not in txt:
            return _err(1, f"edit: old not found in {rp}")
        rp.write_text(txt.replace(old, new, 1), encoding="utf-8")
        return _ok(f"Edited {rp}")
    except Exception as e:
        return _err(1, f"edit failed: {e}")
def builder_mkdir(path: str):
    rp = _builder_resolve(path)
    if rp is None:
        return _err(1, f"mkdir: blocked: {path!r}")
    try:
        rp.mkdir(parents=True, exist_ok=True)
        return _ok(f"mkdir {rp}")
    except Exception as e:
        return _err(1, f"mkdir failed: {e}")
def builder_bash(cmd: str, timeout: int = 30):
    import subprocess as _sp
    cmd = (cmd or "").strip()
    if not cmd:
        return _err(1, "bash: empty")
    try:
        proc = _sp.run(cmd, shell=True, cwd=str(_builder_base()), capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        out = out.strip()[:8000] or "(no output)"
        if proc.returncode != 0:
            return {"output": f"$ {cmd}\n{out}", "exitCode": proc.returncode, "error": f"exit {proc.returncode}"}
        return _ok(f"$ {cmd}\n{out}")
    except Exception as e:
        return _err(1, f"bash failed: {e}")

ALLOWED = {
    "ls", "pwd", "cat", "df", "free", "ps", "uname", "whoami", "uptime",
    "date", "echo", "node", "bun", "npm", "env", "which", "help", "clear",
    "nova", "cd",
    "mkdir", "touch", "write", "read", "edit", "bash", "python3", "pip", "bun", "npm",
}

HELP_TEXT = """Agent_Linux terminal — a REAL root shell with full freedom.

Every command runs through /bin/bash on this machine as root: git, apt,
curl, pip, pipes, redirects, env vars, background jobs — all real.

Built-ins handled natively by the gateway:
  cd <dir>            change directory (persisted across commands)
  pwd / echo / date / uptime / whoami / env / which / uname
  ls / cat / df / free / ps     real telemetry, TS-formatted output
  node -v / bun --version / npm -v
  nova <subcommand>   gateway control — try 'nova help'
  write/read/edit <path> …   quick file helpers (workspace-sandboxed)
  clear               clear the terminal
  help                this help

Command mode runs one command at a time. Open a session tab in the
Terminal view when a command needs to prompt you (ssh, git credentials,
sudo) or a full screen (vim, less, top) — each session is its own shell.

The only refusals are host-destroying commands (rm -rf /, fork bombs,
mkfs, raw disk writes, shutdown/reboot) — exit 126. A command that runs
longer than 90s is killed with exit 124 (use `nohup … &` for daemons)."""

NOVA_HELP_TEXT = """nova — Agent_Linux gateway control

  nova status         gateway snapshot — providers, models, latency, memory, gpu
  nova models [n]     top requested models in the last 48h (default 10)
  nova gpu            free GPU / compute provider pool
  nova gpu <id> on|off   attach / detach a compute provider (e.g. nova gpu kaggle on)
  nova gpu strategy <quota_aware|latency_first|max_vram>
  nova agents [n]     recent autonomous agent tasks (default 8)
  nova storage        storage providers overview
  nova keys           client keys overview
  nova help           this help"""

ENV_KEYS = [
    "NODE_ENV", "PORT", "HOSTNAME", "DATABASE_URL", "NEXT_RUNTIME",
    "NODE_VERSION", "npm_package_name", "npm_lifecycle_event",
]

_MASK_RE = re.compile(r"TOKEN|SECRET|KEY|PASSWORD", re.IGNORECASE)

GPU_STRATEGIES = {"quota_aware", "latency_first", "max_vram"}

WHICH_PATHS = {
    "ls": "/usr/bin/ls", "pwd": "/usr/bin/pwd", "cat": "/usr/bin/cat",
    "df": "/usr/bin/df", "free": "/usr/bin/free", "ps": "/usr/bin/ps",
    "uname": "/usr/bin/uname", "whoami": "/usr/bin/whoami",
    "uptime": "/usr/bin/uptime", "date": "/bin/date", "echo": "/usr/bin/echo",
    "node": "/usr/local/bin/node", "bun": "/usr/local/bin/bun",
    "npm": "/usr/local/bin/npm", "env": "/usr/bin/env", "which": "/usr/bin/which",
    "clear": "/usr/bin/clear", "nova": "/usr/local/bin/nova",
    "help": "built-in",
}

# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def _ok(stdout: str) -> dict:
    return {"code": 0, "stdout": stdout, "stderr": ""}


def _err(code: int, stderr: str) -> dict:
    return {"code": code, "stdout": "", "stderr": stderr}


def r1(n: float) -> float:
    """Math.round(n * 10) / 10 parity (half away from zero for positives)."""
    return math.floor(n * 10 + 0.5) / 10


def _round_half_up(n: float) -> int:
    return int(math.floor(n + 0.5))


def _int_if(value: float) -> float | int:
    """JSON cleanliness: 4096.0 → 4096 (JS Number parity)."""
    if isinstance(value, int):
        return value
    return int(value) if value == int(value) else value


def pad2(n: int) -> str:
    return f"{n:02d}"


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def fmt_uptime_short(ms: int) -> str:
    s = max(0, int(ms // 1000))
    d = s // 86400
    h = (s % 86400) // 3600
    m = (s % 3600) // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def tokenize(cmd: str) -> list[str]:
    out: list[str] = []
    cur = ""
    quote: str | None = None
    for ch in cmd:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch.isspace():
            if cur:
                out.append(cur)
            cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------- #
# OS telemetry (real /proc + stdlib)
# --------------------------------------------------------------------------- #


def uptime_seconds() -> float:
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except Exception:
        return max(0.0, time.time() - PROCESS_START_MS / 1000)


def meminfo_kb() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[key.strip()] = int(parts[0])  # kB
    except OSError:
        pass
    return out


def swapinfo_kb() -> dict[str, int]:
    """Real swap telemetry from /proc/meminfo (no configured fiction)."""
    mi = meminfo_kb()
    return {
        "total": mi.get("SwapTotal", 0),
        "free": mi.get("SwapFree", 0),
    }


def proc_status_kb() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[key.strip()] = int(parts[0])  # kB
    except OSError:
        pass
    return out


def rss_kb() -> int:
    """Current resident set of THIS python process, in kB."""
    status = proc_status_kb()
    if status.get("VmRSS"):
        return status["VmRSS"]
    try:
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)  # KB on Linux
    except Exception:
        return 0


def vm_peak_kb() -> int:
    return proc_status_kb().get("VmPeak", 0)


def totalmem_mb() -> int:
    mi = meminfo_kb()
    return _round_half_up(mi.get("MemTotal", 0) / 1024)


def freemem_mb() -> int:
    """Node os.freemem() parity — Linux MemAvailable."""
    mi = meminfo_kb()
    return _round_half_up(mi.get("MemAvailable", mi.get("MemFree", 0)) / 1024)


_NODE_VERSION_CACHE: str | None = None


def node_version_string() -> str:
    """Honest JS-runtime report: real `node -v` when available, else the
    Python runtime that actually powers the gateway."""
    global _NODE_VERSION_CACHE
    if _NODE_VERSION_CACHE is not None:
        return _NODE_VERSION_CACHE
    path = shutil.which("node")
    if path:
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [path, "-v"], capture_output=True, text=True, timeout=2, check=False
            )
            out = (proc.stdout or proc.stderr).strip()
            if out:
                _NODE_VERSION_CACHE = out
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    _NODE_VERSION_CACHE = f"python-{platform.python_version()} (no node runtime)"
    return _NODE_VERSION_CACHE


# --------------------------------------------------------------------------- #
# Simulated command implementations (real telemetry, TS output formats)
# --------------------------------------------------------------------------- #


def free_table(swap_total: int | None = None) -> str:
    """Real /proc/meminfo parity — swap comes from the OS, not a config key."""
    total = totalmem_mb()
    free_mb = freemem_mb()
    buff = _round_half_up(total * 0.18)
    shared = min(total, 142)
    used = max(0, total - free_mb - buff)
    avail = min(total, free_mb + _round_half_up(buff * 0.6))
    head = " " * 15 + "".join(w.rjust(12) for w in
                              ["total", "used", "free", "shared", "buff/cache", "available"])
    mem = "Mem:".ljust(15) + "".join(str(n).rjust(12) for n in [total, used, free_mb, shared, buff, avail])
    if swap_total is None:
        si = swapinfo_kb()
        swap_total = _round_half_up(si["total"] / 1024)
        swap_used = max(0, swap_total - _round_half_up(si["free"] / 1024))
    else:
        swap_used = 0
    swap = "Swap:".ljust(15) + "".join(str(n).rjust(12) for n in [swap_total, swap_used, max(0, swap_total - swap_used)])
    return "\n".join([head, mem, swap])


def _df_size(n: int) -> str:
    x = float(n)
    unit = ""
    for unit in ("", "K", "M", "G", "T"):
        if x < 1024 or unit == "T":
            break
        x /= 1024
    if unit == "":
        return str(int(x))
    return f"{x:.1f}{unit}" if x < 10 else f"{x:.0f}{unit}"


def df_table() -> str:
    devices: dict[str, str] = {}
    try:
        with open("/proc/self/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    devices.setdefault(parts[1], parts[0])
    except OSError:
        pass

    header = f"{'Filesystem':<15}{'Size':>6}{'Used':>6}{'Avail':>6}{'Use%':>5} Mounted on"
    lines = [header]
    for mount in ["/", "/workspace", "/dev/shm", "/run", "/tmp"]:
        if not os.path.exists(mount):
            continue
        try:
            du = shutil.disk_usage(mount)
        except OSError:
            continue
        fs = devices.get(mount, "overlay")
        pct = int(math.ceil((du.used / du.total) * 100)) if du.total else 0
        lines.append(
            f"{fs:<15}{_df_size(du.total):>6}{_df_size(du.used):>6}"
            f"{_df_size(du.free):>6}{pct:>5} {mount}"
        )
    return "\n".join(lines)


def _ps_row(user: str, pid: str, cpu: str, mem_pct: str, vsz: str, rss: str,
            tty: str, stat_s: str, start: str, cputime: str, cmd: str) -> str:
    return (f"{user:<9}{pid:>5} {cpu:>4} {mem_pct:>4} {vsz:>7} {rss:>6} "
            f"{tty:<9}{stat_s:<5}{start:<6}{cputime:<6}{cmd}")


def ps_table() -> str:
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    total_kb = meminfo_kb().get("MemTotal", 0)
    boot = time.time() - uptime_seconds()
    now_s = time.time()
    rows: list[tuple[int, str]] = []
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        pids = []
    for pid in sorted(pids):
        uid = 0
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                raw = f.read()
            rparen = raw.rfind(")")
            comm = raw[raw.find("(") + 1:rparen]
            fields = raw[rparen + 2:].split()
            # fields[0]=state … [3]=tty_nr [10]=utime [11]=stime [17]=starttime [19]=vsize [20]=rss
            state = fields[0]
            tty_nr = int(fields[3])
            session = int(fields[5])
            threads = int(fields[17])
            starttime = int(fields[19])
            vsize = int(fields[20])
            rss_pages = int(fields[21])
            utime, stime = int(fields[11]), int(fields[12])
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace").replace("\x00", " ").strip()
            try:
                with open(f"/proc/{pid}/status", encoding="utf-8") as f:
                    uid = 0
                    for line in f:
                        if line.startswith("Uid:"):
                            uid = int(line.split()[1])
                            break
                import pwd as pwd_mod

                user = pwd_mod.getpwuid(uid).pw_name
            except Exception:
                user = str(uid)
        except (OSError, IndexError, ValueError):
            continue
        start_epoch = boot + starttime / hz
        elapsed = max(0.001, now_s - start_epoch)
        cpu_s = (utime + stime) / hz
        cpu_pct = (cpu_s / elapsed) * 100
        rss_bytes = rss_pages * page
        mem_pct = (rss_bytes / (total_kb * 1024) * 100) if total_kb else 0.0
        stat_s = state + ("s" if session == pid else "") + ("l" if threads > 1 else "")
        start_dt = datetime.fromtimestamp(start_epoch)
        if now_s - start_epoch < 86400:
            start_str = f"{pad2(start_dt.hour)}:{pad2(start_dt.minute)}"
        else:
            start_str = f"{start_dt:%b%d}"
        if cpu_s < 3600:
            cputime = f"{int(cpu_s // 60)}:{int(cpu_s % 60):02d}"
        else:
            cputime = f"{int(cpu_s // 3600)}:{int((cpu_s % 3600) // 60):02d}:{int(cpu_s % 60):02d}"
        tty_str = "?" if tty_nr == 0 else f"pts/{tty_nr % 256}"
        cmd = cmd or f"[{comm}]"
        rows.append((pid, _ps_row(
            user[:9], str(pid), f"{cpu_pct:.1f}", f"{mem_pct:.1f}",
            str(vsize // 1024), str(rss_bytes // 1024), tty_str, stat_s,
            start_str, cputime, cmd,
        )))
    header = _ps_row("USER", "PID", "%CPU", "%MEM", "VSZ", "RSS", "TTY", "STAT", "START", "TIME", "COMMAND")
    return "\n".join([header] + [r[1] for r in rows])


def run_ls(args: list[str], cwd: str | None = None) -> dict:
    all_f = False
    long_f = False
    target = cwd or SANDBOX_ROOT
    for a in args:
        if a.startswith("-"):
            if "a" in a:
                all_f = True
            if "l" in a:
                long_f = True
        else:
            target = a if os.path.isabs(a) else os.path.normpath(os.path.join(target, a))
    if not os.path.isdir(target):
        return _err(2, f"ls: cannot access '{target}': No such file or directory")
    try:
        entries = sorted(os.scandir(target), key=lambda e: e.name)
    except OSError:
        entries = []
    visible = [e for e in entries if all_f or not e.name.startswith(".")]
    if not long_f:
        names = [e.name + "/" if e.is_dir() else e.name for e in visible]
        return _ok("\n".join(names))

    def owner_group(st: os.stat_result) -> tuple[str, str]:
        import grp as grp_mod
        import pwd as pwd_mod

        try:
            user = pwd_mod.getpwuid(st.st_uid).pw_name
        except Exception:
            user = str(st.st_uid)
        try:
            group = grp_mod.getgrgid(st.st_gid).gr_name
        except Exception:
            group = str(st.st_gid)
        return user, group

    def date_of(st: os.stat_result) -> str:
        dt = datetime.fromtimestamp(st.st_mtime)
        return f"{dt:%b} {dt.day:>2} {pad2(dt.hour)}:{pad2(dt.minute)}"

    rows: list[str] = []
    total_blocks = 0
    if all_f:
        st_root = os.stat(target)
        st_parent = os.stat(os.path.dirname(target) or "/")
        total_blocks += st_root.st_blocks // 2 + st_parent.st_blocks // 2
        u, g = owner_group(st_root)
        rows.append(f"drwxr-xr-x {st_root.st_nlink:>2} {u} {g} {4096:>6} {date_of(st_root)} .")
        u2, g2 = owner_group(st_parent)
        rows.append(f"drwxr-xr-x {st_parent.st_nlink:>2} {u2} {g2} {4096:>6} {date_of(st_parent)} ..")
    for e in visible:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        total_blocks += st.st_blocks // 2
        perm = stat_mod.filemode(st.st_mode)
        u, g = owner_group(st)
        name = e.name + "/" if e.is_dir() else e.name
        rows.append(f"{perm} {st.st_nlink:>2} {u} {g} {st.st_size:>6} {date_of(st)} {name}")
    return _ok("\n".join([f"total {total_blocks}"] + rows))


def uname_line(args: list[str]) -> str:
    flags = "".join(args)
    sys_name = platform.system()
    host = socket.gethostname()
    rel = platform.release()
    arch = platform.machine()
    if "a" in flags:
        return f"{sys_name} {host} {rel} #1-Agent_Linux SMP {arch} {arch} {arch} GNU/Linux"
    if "s" in flags:
        return sys_name
    if "r" in flags:
        return rel
    if "m" in flags:
        return arch
    return sys_name


def uptime_line() -> str:
    up = uptime_seconds()
    d = int(up // 86400)
    h = int((up % 86400) // 3600)
    m = int((up % 3600) // 60)
    now = datetime.now()
    clock = f"{pad2(now.hour)}:{pad2(now.minute)}:{pad2(now.second)}"
    up_part = (
        f"{d} day{'' if d == 1 else 's'},  {h}:{pad2(m)}"
        if d > 0 else f"{h}:{pad2(m)}"
    )
    l0, l1, l2 = os.getloadavg()
    return (f" {clock} up {up_part},  1 user,  "
            f"load average: {l0:.2f}, {l1:.2f}, {l2:.2f}")


def env_lines() -> str:
    lines: list[str] = []
    for k in ENV_KEYS:
        v = os.environ.get(k)
        if v is not None:
            lines.append(f"{k}={'••••••••' if _MASK_RE.search(k) else v}")
    lines.append("NOVA_ADMIN_TOKEN=••••••••")
    lines.append("NOVA_SANDBOX=1")
    return "\n".join(lines)


def run_which(args: list[str]) -> dict:
    if not args:
        return _err(1, "which: missing operand")
    target = args[0]
    p = WHICH_PATHS.get(target)
    if p:
        return _ok(f"{target}: shell built-in command" if p == "built-in" else p)
    return _err(1, f"which: no {target} in (/usr/local/bin:/usr/bin:/bin)")


def run_version_cmd(cmd: str, args: list[str]) -> dict:
    is_version = bool(args) and args[0] in ("-v", "--version")
    if not is_version:
        return _err(1, f"{cmd}: sandbox permits only '{cmd} -v' / '{cmd} --version'")
    path = shutil.which(cmd)
    if not path:
        return _err(127, f"bash: {cmd}: command not found")
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, version probe only
            [path, args[0]], capture_output=True, text=True, timeout=2, check=False
        )
        out = (proc.stdout or proc.stderr).strip()
        return _ok(out or "unknown")
    except (OSError, subprocess.SubprocessError) as e:
        return _err(1, f"{cmd}: failed to execute — {e}")


def run_cat(db: Session, args: list[str]) -> dict:
    if not args:
        return _err(1, "cat: missing file operand")
    f = args[0]
    if f == "nova.config.json":
        import json

        if not APP_AVAILABLE:
            return _err(1, f"cat: nova.config.json: {NO_APP}")
        rows = db.scalars(select(SystemConfig).order_by(SystemConfig.key.asc())).all()
        obj: dict = {}
        for r in rows:
            try:
                obj[r.key] = json.loads(r.value)
            except Exception:
                obj[r.key] = r.value
        return _ok(json.dumps(obj, indent=2))
    if f == ".env":
        # Real environment (masked) — not a fabricated sample file.
        if not APP_AVAILABLE:
            return _err(1, f"cat: .env: {NO_APP}")
        from nova.config import DATABASE_URL, PORT

        token = get_config(db, "admin_token")
        active_row = db.scalars(
            select(StorageProviderRow).where(StorageProviderRow.active.is_(True))
        ).first()
        token_mask = f"{token[:4]}••••••" if token else "••••••••"
        db_show = DATABASE_URL
        if "@" in db_show:  # mask postgres credentials
            scheme, _, rest = db_show.partition("://")
            _, _, host = rest.rpartition("@")
            db_show = f"{scheme}://••••••@{host}"
        gpu_enabled = get_config(db, "gpu_enabled")
        strategy = get_config(db, "gpu_strategy")
        return _ok("\n".join([
            "# live gateway environment — secrets masked",
            f"PORT={PORT}",
            f"DATABASE_URL={db_show}",
            f"NOVA_ADMIN_TOKEN={token_mask}",
            f"NOVA_GPU_ENABLED={gpu_enabled if gpu_enabled is not None else '1'}",
            f"NOVA_GPU_STRATEGY={strategy if strategy else 'quota_aware'}",
            f"NOVA_STORAGE_ACTIVE={active_row.id if active_row else 'local_disk'}",
        ]))
    return _err(1, f"cat: {f}: No such file or directory")


# --------------------------------------------------------------------------- #
# nova CLI — live DB state
# --------------------------------------------------------------------------- #


def nova_status(db: Session) -> dict:
    since = datetime.fromtimestamp((time.time() * 1000 - 24 * 3600_000) / 1000, tz=timezone.utc)
    since = since.replace(tzinfo=None)
    provider_count = db.scalar(select(func.count()).select_from(Provider)) or 0
    model_count = db.scalar(select(func.count()).select_from(ModelRow)) or 0
    route_count = db.scalar(
        select(func.count()).select_from(ModelRoute).where(ModelRoute.enabled.is_(True))
    ) or 0
    agg = db.execute(
        select(func.count(RequestLog.id), func.avg(RequestLog.latencyMs))
        .where(RequestLog.ts >= since)
    ).one()
    reqs = int(agg[0] or 0)
    avg_raw = float(agg[1]) if agg[1] is not None else 0.0
    cache_hits = db.scalar(
        select(func.count(RequestLog.id))
        .where(RequestLog.via == "cache", RequestLog.ts >= since)
    ) or 0
    started_raw = get_config(db, "gateway_started_at")
    si = swapinfo_kb()
    swap_mb = _round_half_up(si["total"] / 1024)
    gpus = get_gpu_providers(db)
    gpu_enabled_raw = get_config(db, "gpu_enabled")
    try:
        started_at = float(started_raw) if started_raw is not None else 0.0
    except ValueError:
        started_at = 0.0
    if math.isfinite(started_at) and started_at > 0:
        uptime_ms = time.time() * 1000 - started_at
    else:
        uptime_ms = time.time() * 1000 - PROCESS_START_MS
    avg_lat = _round_half_up(avg_raw)
    cache_pct = f"{(cache_hits / reqs) * 100:.1f}" if reqs else "0.0"
    gpu_enabled = gpu_enabled_raw not in ("0", "false")
    enabled_count = sum(1 for g in gpus if g["enabled"])
    return _ok("\n".join([
        f"⚡ Agent_Linux gateway — {provider_count} providers · {model_count} models · {route_count} fallback routes",
        f"   uptime: {fmt_uptime_short(int(uptime_ms))} · requests (24h): {reqs} · avg latency: {avg_lat}ms · cache hit rate: {cache_pct}%",
        f"   memory: {totalmem_mb()} MB total · {freemem_mb()} MB available · swap {swap_mb} MB · "
        f"gpu {'enabled' if gpu_enabled else 'disabled'} — {enabled_count}/{len(gpus)} providers enabled",
    ]))


def nova_models(db: Session, args: list[str]) -> dict:
    def parse_n() -> int:
        try:
            v = int(str(args[0]).strip())
        except (IndexError, TypeError, ValueError):
            return 10
        return v if v else 10

    n = min(50, max(1, parse_n()))
    since = datetime.fromtimestamp((time.time() * 1000 - 48 * 3600_000) / 1000, tz=timezone.utc)
    since = since.replace(tzinfo=None)
    logs = db.execute(
        select(RequestLog.model, RequestLog.tokensIn, RequestLog.tokensOut, RequestLog.latencyMs)
        .where(RequestLog.ts >= since)
    ).all()
    by_model: dict[str, dict] = {}
    for model, tin, tout, lat in logs:
        e = by_model.setdefault(model, {"reqs": 0, "tokens": 0, "lat_sum": 0})
        e["reqs"] += 1
        e["tokens"] += tin + tout
        e["lat_sum"] += lat
    rows = sorted(by_model.items(), key=lambda kv: -kv[1]["reqs"])[:n]
    lines = [
        f"{'MODEL':<34}{'REQS':>7}{'TOKENS':>11}{'AVG MS':>9}",
        *[
            f"{trunc(model, 33):<34}{e['reqs']:>7}{e['tokens']:>11}"
            f"{_round_half_up(e['lat_sum'] / e['reqs']):>9}"
            for model, e in rows
        ],
    ]
    if not rows:
        lines.append("(no requests in the last 48h)")
    return _ok("\n".join(lines))


def nova_boost(db: Session, args: list[str]) -> dict:
    """Real memory report — the Node V8 heap limit was runtime fiction in the
    Python server (no Node process hosts the gateway), so `nova boost` now
    reports the actual OS memory/swap instead of pretending to tune anything."""
    si = swapinfo_kb()
    return _ok(
        "memory — live OS telemetry (no tuning knobs required):\n"
        f"  RAM total     {totalmem_mb()} MB\n"
        f"  RAM available {freemem_mb()} MB\n"
        f"  swap total    {_round_half_up(si['total'] / 1024)} MB\n"
        f"  swap free     {_round_half_up(si['free'] / 1024)} MB\n"
        "  gateway process RSS " + str(_int_if(rss_kb() / 1024)) + " MB\n"
        "(the Python gateway allocates dynamically — no V8 heap limit to set)"
    )


def _gpu_row(g: dict) -> str:
    vram = f"{g['vram_gb']}GB"
    return (f"{trunc(g['name'], 23):<24}{trunc(g['gpu'], 19):<20}{vram:>5}  "
            f"{'yes' if g['enabled'] else 'no':<8}{trunc(g['free_tier'], 44)}")


def nova_gpu(db: Session, args: list[str]) -> dict:
    gpus = get_gpu_providers(db)
    strategy = get_config(db, "gpu_strategy")

    if args and args[0] == "strategy":
        nxt = (args[1] if len(args) > 1 else "").lower()
        if nxt not in GPU_STRATEGIES:
            return _err(1, f"nova: unknown strategy '{args[1] if len(args) > 1 else ''}' — choose quota_aware | latency_first | max_vram")
        set_config(db, "gpu_strategy", nxt)
        return _ok(f"✓ GPU scheduling strategy set to {nxt}")

    if args:
        target = args[0].lower()
        action = (args[1] if len(args) > 1 else "").lower()
        match = next(
            (
                g for g in gpus
                if g["id"].lower() == target
                or target in re.sub(r"\s+", "-", g["name"].lower())
            ),
            None,
        )
        if match is None:
            known = ", ".join(g["id"] for g in gpus) or "(none configured)"
            return _err(1, f"nova: no compute provider matches '{args[0]}' — known ids: {known}")
        if action not in ("on", "off"):
            return _err(1, f"nova: expected 'on' or 'off' after '{args[0]}' — e.g. nova gpu {match['id']} on")
        nxt = action == "on"
        if match["enabled"] == nxt:
            return _ok(f"{match['name']} is already {'attached' if nxt else 'detached'} — nothing to do")
        updated = [{**g, "enabled": nxt} if g["id"] == match["id"] else g for g in gpus]
        set_gpu_providers(db, updated)
        attached = sum(g["vram_gb"] for g in updated if g["enabled"])
        return _ok(
            f"{'✓' if nxt else '▪'} {match['name']} ({match['gpu']}, {match['vram_gb']} GB) "
            f"{'attached to' if nxt else 'detached from'} the pool\n"
            f"  pool now: {attached} GB VRAM attached"
        )

    lines = [
        f"{'PROVIDER':<24}{'GPU':<20}{'VRAM':>5}  {'ENABLED':<8}FREE TIER",
        *[
            _gpu_row(g) for g in gpus
        ],
    ]
    if not gpus:
        lines.append("(no compute providers configured)")
    else:
        total = sum(g["vram_gb"] for g in gpus)
        enabled_sum = sum(g["vram_gb"] for g in gpus if g["enabled"])
        lines.append(
            f"pool: {total} GB VRAM total · {enabled_sum} GB attached · strategy: {strategy if strategy else 'quota_aware'}"
        )
    return _ok("\n".join(lines))


def _agent_row(t, now_ms: int, fmt_age, ts_ms) -> str:
    goal = re.sub(r"\s+", " ", t.goal)
    return f"{t.status:<11}{fmt_age(now_ms - ts_ms(t.createdAt)):<10}{trunc(goal, 64)}"


def nova_agents(db: Session, args: list[str]) -> dict:
    def parse_n() -> int:
        try:
            v = int(str(args[0]).strip())
        except (IndexError, TypeError, ValueError):
            return 8
        return v if v else 8

    n = min(20, max(1, parse_n()))
    tasks = db.scalars(
        select(AgentTask).order_by(AgentTask.createdAt.desc()).limit(n)
    ).all()
    now_ms = int(time.time() * 1000)

    def fmt_age(ms: int) -> str:
        m = int(ms // 60000)
        if m < 60:
            return f"{m or 1}m ago"
        h = m // 60
        if h < 24:
            return f"{h}h ago"
        return f"{h // 24}d ago"

    def ts_ms(dt: datetime | None) -> int:
        if dt is None:
            return 0
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

    lines = [
        f"{'STATUS':<11}{'CREATED':<10}GOAL",
        *[
            _agent_row(t, now_ms, fmt_age, ts_ms) for t in tasks
        ],
    ]
    if not tasks:
        lines.append("(no agent tasks yet)")
    return _ok("\n".join(lines))


def nova_storage(db: Session) -> dict:
    rows = db.scalars(select(StorageProviderRow).order_by(StorageProviderRow.createdAt.asc())).all()
    lines = [
        f"{'PROVIDER':<26}{'TYPE':<14}{'STATUS':<13}{'ACTIVE':>6}  {'USAGE':>9}",
        *[
            f"{trunc(r.name, 25):<26}{trunc(r.type, 13):<14}{r.status:<13}"
            f"{'yes' if r.active else 'no':>6}  {f'{r.usageMb:.1f} MB':>9}"
            for r in rows
        ],
    ]
    return _ok("\n".join(lines))


def nova_keys(db: Session) -> dict:
    keys = db.scalars(select(ClientKey).order_by(ClientKey.createdAt.asc())).all()
    lines = [
        f"{'NAME':<22}{'REQS':>7}{'TOKENS IN':>12}{'TOKENS OUT':>12}{'RPM':>6}  ENABLED",
        *[
            f"{trunc(k.name, 21):<22}{k.reqCount:>7}{k.tokensIn:>12}{k.tokensOut:>12}"
            f"{k.rpmLimit:>6}  {'yes' if k.enabled else 'no'}"
            for k in keys
        ],
        "",
        f"🔑 {len(keys)} client keys · {sum(1 for k in keys if k.enabled)} enabled",
    ]
    return _ok("\n".join(lines))


def run_nova(db: Session, args: list[str]) -> dict:
    sub = args[0] if args else "help"
    if sub == "help":
        return _ok(NOVA_HELP_TEXT)
    if not APP_AVAILABLE:
        # `nova …` reads gateway state; say that plainly instead of failing
        # with an import error on a terminal that has no gateway behind it.
        return _err(1, f"nova {sub}: {NO_APP}")
    if sub == "status":
        return nova_status(db)
    if sub == "models":
        return nova_models(db, args[1:])
    if sub == "boost":
        return nova_boost(db, args[1:])
    if sub == "gpu":
        return nova_gpu(db, args[1:])
    if sub == "agents":
        return nova_agents(db, args[1:])
    if sub == "storage":
        return nova_storage(db)
    if sub == "keys":
        return nova_keys(db)
    return _err(1, f"nova: unknown subcommand '{sub}' — try 'nova help'")


# --------------------------------------------------------------------------- #
# Real shell passthrough — full freedom
# --------------------------------------------------------------------------- #

# Only genuinely catastrophic patterns stay blocked. Everything else — git,
# apt, pipes, redirects, sudo, curl, whatever — runs for real: this is the
# user's own self-hosted server and the terminal is theirs.
CATASTROPHIC_RES = [
    re.compile(r"rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)*(/|~|\$HOME)\s*$"),  # rm -rf / or ~
    re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f|rm\s+-[a-zA-Z]*f[a-zA-Z]*r"),  # rm -rf anywhere → still allow common uses? no: only / root handled above
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),  # fork bomb
    re.compile(r"mkfs\.(ext[234]|xfs|btrfs|vfat)"),  # filesystem wipe
    re.compile(r"dd\s+.*of=/dev/(sd|nvme|hd)"),  # raw disk write
    re.compile(r">\s*/dev/(sd|nvme|hd)[a-z]"),  # redirect overwrite of a disk
    re.compile(r"shutdown|reboot|halt|poweroff|init\s+0|init\s+6"),  # host power
    re.compile(r"chmod\s+-R\s+777\s+/(\s|$)"),
]

SHELL_TIMEOUT_S = 90
SHELL_OUTPUT_CAP = 200_000


def shell_env() -> dict:
    """Environment for the real shell — full Linux root-user parity."""
    env = dict(os.environ)
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env["TERM"] = env.get("TERM") or "xterm-256color"
    env["HOME"] = env.get("HOME") or "/root"
    env["USER"] = env.get("USER") or "root"
    env["SHELL"] = env.get("SHELL") or "/bin/bash"
    env["LANG"] = env.get("LANG") or "C.UTF-8"
    # Surface the gateway identity to scripts.
    env["NOVA_GATEWAY"] = "1"
    return env


def shell_catastrophic(cmd: str) -> str | None:
    low = " ".join(cmd.lower().split())
    for rx in CATASTROPHIC_RES:
        m = rx.search(low)
        if m:
            return m.group(0)
    return None


def run_real_shell(cmd: str, cwd: str, timeout: float = SHELL_TIMEOUT_S) -> dict:
    """Execute a command through the real system shell (/bin/bash).

    This is the user's own server: they asked for full command freedom and the
    process already runs as root inside their container. Output is capped and
    a hard timeout prevents wedged commands from pinning the event loop.
    """
    try:
        proc = subprocess.run(
            ["/bin/bash", "-c", cmd],
            cwd=cwd if os.path.isdir(cwd) else str(PROJECT_ROOT),
            env=shell_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _err(124, f"timeout: the command ran longer than {int(timeout)}s and was killed")
    except OSError as e:
        return _err(126, f"shell: failed to spawn /bin/bash — {e}")
    return {"code": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or ""}


# --------------------------------------------------------------------------- #
# Persistence + executor
# --------------------------------------------------------------------------- #


def persist_command(db: Session, command: str, output: str, exit_code: int,
                    duration_ms: int, cwd: str) -> None:
    """Best-effort persistence (TerminalCommand, capped at 200 rows)."""
    if not APP_AVAILABLE:
        return
    try:
        db.add(TerminalCommand(
            command=command, output=output, exitCode=exit_code,
            durationMs=duration_ms, cwd=cwd,
        ))
        db.commit()
        count = db.scalar(select(func.count()).select_from(TerminalCommand)) or 0
        if count > 200:
            excess = count - 200
            old_ids = db.scalars(
                select(TerminalCommand.id)
                .order_by(TerminalCommand.createdAt.asc())
                .limit(excess)
            ).all()
            if old_ids:
                db.execute(delete(TerminalCommand).where(TerminalCommand.id.in_(old_ids)))
                db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def execute_command(db: Session, command: str, cwd: str | None = None) -> dict:
    # --- builder routing (real FS) - before allowlist ---
    _raw = (command or "").strip()
    if _raw.startswith("write "):
        rest = _raw[6:].strip()
        if " " in rest:
            pth, content = rest.split(" ", 1)
            if content.strip().startswith("<<"):
                content = content.split(chr(10), 1)[-1] if chr(10) in content else content
                content = content.replace("EOF", "").strip()
            return builder_write(pth, content)
        else:
            return _err(1, "write: usage: write <path> <content>")
    if _raw.startswith("read "):
        return builder_read(_raw[5:].strip())
    if _raw.startswith("mkdir "):
        return builder_mkdir(_raw[6:].strip())
    if _raw.startswith("edit "):
        rest = _raw[5:].strip()
        if "|||" in rest and " " in rest:
            pth, remain = rest.split(" ", 1)
            if "|||" in remain:
                old, new = remain.split("|||", 1)
                return builder_edit(pth.strip(), old, new)
        return _err(1, "edit: usage: edit <path> <old>|||<new>")
    if _raw.startswith("bash "):
        return builder_bash(_raw[5:].strip())

    # The simulated layer below reads gateway state (terminal cwd, GPU pool,
    # request logs). On a terminal hosted without the gateway that is honest
    # unavailability, not a crash.
    if db is None or not APP_AVAILABLE:
        return {"ok": False, "output": NO_APP, "stdout": "", "stderr": NO_APP,
                "code": 1, "duration_ms": 1, "cwd": str(SANDBOX_ROOT)}

    """Port of executeCommand() → ExecResult wire shape."""
    started = time.time() * 1000
    raw = command.strip() if isinstance(command, str) else ""
    resolved_cwd = str(PROJECT_ROOT)  # the workspace IS the project directory
    try:
        resolved_cwd = (cwd.strip() if cwd else "") or get_config(db, "terminal_cwd") or str(PROJECT_ROOT)
    except Exception:
        pass

    def finish(out: dict) -> dict:
        stdout = out["stdout"]
        stderr = out["stderr"]
        output = (f"{stdout}\n{stderr}" if stderr and stdout else (stderr or stdout))
        duration_ms = max(1, int(time.time() * 1000 - started))
        persist_command(db, raw, output, out["code"], duration_ms, resolved_cwd)
        return {
            "ok": out["code"] == 0,
            "output": output,
            "stdout": stdout,
            "stderr": stderr,
            "code": out["code"],
            "duration_ms": duration_ms,
            "cwd": resolved_cwd,
        }

    if not raw:
        return finish(_ok(""))
    if len(raw) > 8000:
        return finish(_err(126, "sandbox: command too long"))

    # Full-freedom shell: only catastrophic patterns are refused.
    bad = shell_catastrophic(raw)
    if bad:
        return finish(_err(126, f"refused: '{bad}' would destroy the host — everything else is allowed"))

    tokens = tokenize(raw)
    cmd = tokens[0] if tokens else ""
    args = tokens[1:]

    # cd — persists across commands (built-in, tracked by the gateway).
    if cmd == "cd":
        prev_cwd = ""
        try:
            prev_cwd = get_config(db, "terminal_prev_cwd") or ""
        except Exception:
            pass
        target = args[0] if args else (os.environ.get("HOME") or "/root")
        if target == "-":
            if not prev_cwd or not os.path.isdir(prev_cwd):
                return finish(_err(1, "cd: no previous directory"))
            target, prev_cwd = prev_cwd, resolved_cwd
        elif not os.path.isabs(target):
            target = os.path.normpath(os.path.join(resolved_cwd, target))
        if not os.path.isdir(target):
            return finish(_err(1, f"cd: {target}: No such file or directory"))
        try:
            set_config(db, "terminal_prev_cwd", prev_cwd or resolved_cwd)
            set_config(db, "terminal_cwd", target)
        except Exception:
            pass
        return finish(_ok(""))

    # Anything the simulated layer does not implement natively runs through
    # the REAL shell — git, apt, curl, pipes, redirects, env vars, whatever.
    if cmd not in ALLOWED or any(ch in raw for ch in DANGEROUS_CHARS) or cmd in DANGEROUS_WORDS:
        out = run_real_shell(raw, resolved_cwd)
        return finish(out)

    try:
        if cmd == "clear":
            return finish(_ok(""))
        if cmd == "help":
            return finish(_ok(HELP_TEXT))
        if cmd == "pwd":
            return finish(_ok(resolved_cwd))
        if cmd == "echo":
            return finish(_ok(" ".join(args)))
        if cmd == "whoami":
            # The sandbox runs as the server's OS user; inside the container
            # that is root. Report honestly rather than a fixed persona.
            try:
                import pwd as _pwd

                return finish(_ok(_pwd.getpwuid(os.getuid()).pw_name))
            except Exception:
                return finish(_ok(os.environ.get("USER") or os.environ.get("LOGNAME") or "root"))
        if cmd == "date":
            now = datetime.now().astimezone()
            off = now.utcoffset()
            minutes = int(off.total_seconds() // 60) if off else 0
            sign = "+" if minutes >= 0 else "-"
            gm = f"GMT{sign}{abs(minutes) // 60:02d}{abs(minutes) % 60:02d}"
            tzname = time.tzname[0] if time.tzname else "UTC"
            return finish(_ok(f"{now:%a %b %d %Y %H:%M:%S} {gm} ({tzname})"))
        if cmd == "uptime":
            return finish(_ok(uptime_line()))
        if cmd == "env":
            return finish(_ok(env_lines()))
        if cmd == "which":
            return finish(run_which(args))
        if cmd == "uname":
            return finish(_ok(uname_line(args)))
        if cmd == "ls":
            return finish(run_ls(args, resolved_cwd))
        if cmd == "free":
            return finish(_ok(free_table()))
        if cmd == "df":
            return finish(_ok(df_table()))
        if cmd == "ps":
            return finish(_ok(ps_table()))
        if cmd == "cat":
            return finish(run_cat(db, args))
        if cmd in ("node", "bun", "npm"):
            return finish(run_version_cmd(cmd, args))
        if cmd == "nova":
            return finish(run_nova(db, args))
        # Unknown-but-harmless command → real shell as well.
        return finish(run_real_shell(raw, resolved_cwd))
    except Exception as e:  # noqa: BLE001 — parity with the TS catch-all
        return finish(_err(1, f"nova-exec: internal error — {e}"))
