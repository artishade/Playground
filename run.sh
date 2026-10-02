#!/usr/bin/env sh
# NovaRouter terminal host launcher — runs the Root@Build terminal as its own
# service, with no NovaRouter gateway and no database anywhere near it.
#
#   sh ./terminal/run.sh
#
# Then either use it directly (shells + Agentbox at :3100) or point a
# NovaRouter app at it:
#   NOVA_TERMINAL_URL=http://127.0.0.1:3100
#   NOVA_TERMINAL_TOKEN=<shared secret>   # set on BOTH sides
#
# Works from the full checkout and from a copy of `terminal/` on its own: the
# only requirement is that the directory is named `terminal` and this script
# sits inside it. POSIX-safe (dash) — no pipefail, no [[ ]], no source.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

PY="python3"
if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
fi

# The terminal's own dependencies are three (a web server and an HTTP client).
# In the checkout they are already there, so this is a warm no-op; on a host
# that only received `terminal/`, it is the install.
if ! "$PY" -c "import fastapi, uvicorn, httpx" >/dev/null 2>&1; then
  echo "[novarouter] terminal: installing $(basename "$HERE")/requirements.txt into ${PY}"
  if [ "$PY" != "python3" ]; then
    "$PY" -m pip install --no-cache-dir --disable-pip-version-check --quiet -r "$HERE/requirements.txt"
  else
    python3 -m venv .venv 2>/dev/null && PY="$ROOT/.venv/bin/python" \
      || python3 -m pip install --no-cache-dir --disable-pip-version-check --quiet -r "$HERE/requirements.txt" \
      || python3 -m pip install --no-cache-dir --disable-pip-version-check --quiet --user -r "$HERE/requirements.txt"
  fi
fi

# Stop a host left behind by an earlier run; a hard restart cannot fire the
# shutdown hook, and the old process would keep holding the port.
for pid in $(ps -eo pid,args | grep '[t]erminal\.service' | awk '{print $1}'); do
  [ "$pid" = "$$" ] && continue
  [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)" = "$ROOT" ] || continue
  echo "[novarouter] terminal: stopping stale service process ${pid}"
  kill "$pid" 2>/dev/null || true
done

echo "[novarouter] terminal: ${PY} on ${NOVA_TERMINAL_HOST:-0.0.0.0}:${NOVA_TERMINAL_PORT:-${PORT:-3100}}"
# `-m` (never `python3 terminal/service.py`): the module form puts this
# directory's parent on sys.path, which is what `from terminal...` needs.
exec "$PY" -m terminal.service