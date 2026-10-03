#!/usr/bin/env sh
# Terminal host smoke test — proves a deployment is actually serving, without
# opening a browser. Run it against a local host or a public URL:
#
#   sh deploy/smoke.sh                                  # http://127.0.0.1:3100
#   BASE=https://your-space.hf.space TOKEN=… sh deploy/smoke.sh
#
# POSIX sh (dash-safe): no pipefail, no [[ ]], no source.
set -eu

BASE="${BASE:-http://127.0.0.1:${AGENT_LINUX_PORT:-3100}}"
TOKEN="${TOKEN:-${AGENT_LINUX_TOKEN:-}}"
AUTH=""
# `if`, not `[ … ] && …`: the `&&` form returns 1 when TOKEN is empty, which
# under `set -e` would abort the script before the first check.
if [ -n "$TOKEN" ]; then AUTH="X-Nova-Terminal-Token: $TOKEN"; fi

fail=0
say() { printf '%s\n' "$*"; }
check() { # check <label> <expected-status> <actual-status>
  if [ "$2" = "$3" ]; then say "  ok    $1 ($3)"; else say "  FAIL  $1 (expected $2, got $3)"; fail=1; fi
}
code() { # code <path> [extra curl args…] → HTTP status, or 000 if unreachable
  path="$1"; shift
  out=""
  if out=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@" "$BASE$path" 2>/dev/null); then
    :
  else
    out=""
  fi
  if [ -z "$out" ]; then out="000"; fi
  printf '%s' "$out"
}

say "smoke test against $BASE"
[ -n "$TOKEN" ] || say "  note  no TOKEN set — a token-gated host will answer 401 below"

say ""
say "open endpoints"
check "/health"           200 "$(code /health)"
check "/ (console page)"  200 "$(code /)"
check "/static/console.js" 200 "$(code /static/console.js)"

say ""
say "gated endpoints"
if [ -n "$TOKEN" ]; then
  check "GET  /terminal/pty/sessions" 200 "$(code /terminal/pty/sessions -H "$AUTH")"
  check "GET  /agent/providers"       200 "$(code /agent/providers -H "$AUTH")"
else
  say "  skip  no token supplied"
fi

say ""
say "health body"
if command -v python3 >/dev/null 2>&1; then
  curl -s --max-time 15 "$BASE/health" | python3 -c '
import json, sys
try:
    h = json.load(sys.stdin)
except Exception as err:
    print("  FAIL  /health is not JSON:", err); sys.exit(1)
print("  ok    service   ", h.get("service"), h.get("runtime"))
print("  ok    uptime    ", h.get("uptime_s"), "s")
print("  ok    sessions  ", h.get("sessions"), "/", h.get("max_sessions"))
print("  ok    build_root", h.get("build_root"))
print("  ok    auth      ", "required" if h.get("auth_required") else "OPEN (only sane on loopback)")
box = h.get("agentbox") or {}
print("  ok    agentbox  ", "configured" if box.get("configured") else "off (no provider yet)")
'
else
  curl -s --max-time 15 "$BASE/health"; echo
fi

say ""
if [ "$fail" = 0 ]; then say "PASS"; else say "FAIL"; fi
exit "$fail"