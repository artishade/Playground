# Agent_Linux

A **root Linux shell, a live Chromium browser, and an AI agent that drives both** —
in one self-contained package you can deploy anywhere.

Agent_Linux is not a terminal emulator widget. It is a real PTY-backed root shell
(8 concurrent sessions, `vim`, `top`, `ssh`, `sudo` all behave), a real Chromium
page you and the agent share, and an agent that can add its own capabilities by
connecting MCP servers, uploading skills and registering plugins.

```
┌──────────────────────────── console (served at /) ─────────────────────────────┐
│  ┌─ shell ──────────────────────┐  ┌─ AGENTBOX ─────────────────────────────┐  │
│  │ ~ AgentLinux:/app/build#      │  │ you  deploy this and tail the log      │  │
│  │ $ ls                          │  │ bot  opening the deploy tab…           │  │
│  │   README.md  app/  dist/      │  │      $ npm run deploy  ← runs in a tab │  │
│  │                               │  │      $ tail -f /var/log/deploy.log     │  │
│  └───────────────────────────────┘  └────────────────────────────────────────┘  │
│  [shell] [browser]   ← toggle (Alt+B): the same Chromium the agent drives       │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Table of contents

1. [What it is](#what-it-is)
2. [Use cases](#use-cases)
3. [Architecture](#architecture)
4. [Project structure](#project-structure)
5. [Backend logic](#backend-logic)
6. [The HTTP contract](#the-http-contract)
7. [Connection & configuration](#connection--configuration)
8. [Setup](#setup)
9. [Deploy](#deploy)
10. [SSH & accounts](#ssh--accounts)
11. [Security model](#security-model)
12. [Troubleshooting](#troubleshooting)
13. [Renaming from NovaRouter](#renaming-from-novarouter)

---

## What it is

Three products that share one process, one port and one page:

| | What you get |
| --- | --- |
| **Root shell** | Real PTY sessions in `pty.fork()`. Max 8 tabs, OSC 7 working-directory tracking, 30-minute idle reap, labelled agent tabs. Everything a terminal should do — colour, cursor, `vim`, `top`, interactive `ssh` — works because it *is* a terminal. |
| **Live browser** | One long-lived Chromium the user watches as a ~700 ms JPEG frame stream and the agent drives through tools. Clicking the frame clicks the page. The agent's actions appear in the frame you are looking at. |
| **AI agent (Agentbox)** | Any OpenAI-compatible `/v1` endpoint. It runs shell commands **in a tab you can watch**, browses the web, queries your database, and can be extended at runtime with MCP servers, skills and plugins. |

**One rule holds the whole thing together:** `agent_linux/` runs standalone. It
needs no gateway, no dashboard and **no database**. Copy the directory to a host
and `python3 -m agent_linux.service` gives you all three products. A database,
Playwright and an AI provider are all *optional* — each one is detected, and its
absence degrades honestly instead of breaking anything.

---

## Use cases

**1. A cloud dev box you can talk to**
Deploy to a free host, open the console, and you have a root shell in a browser
plus an agent that can `git clone`, install dependencies, run your build, read
the failure, and fix it — while you watch every command land in a tab.

**2. Agentic web automation**
"Log into the dashboard and download last month's invoices." The agent opens the
page, reads the interactive elements, fills the form, clicks submit, and hands
you the file. Because the browser is shared, you can take over the moment it hits
a 2FA prompt.

**3. Scraping and research without an API**
`browser_eval` runs JavaScript in a real page, so the agent extracts structured
data from sites that have no API and no clean HTML — the same way you would with
the devtools console open.

**4. A personal ops assistant**
Connect the GitHub MCP server and a Postgres MCP server, upload a `deploy.md`
skill, and the agent can open PRs, query production read-only, and follow *your*
runbook rather than guessing.

**5. Sandboxed code execution for your own product**
The HTTP contract is stable and token-gated: build a UI, a bot, or a CI job that
asks Agent_Linux to run something and read the output. The agent's `run_command`
and the `/run` route both execute in the same visible, stateful shells.

**6. Teaching and demos**
Two people can watch the same session: one drives the browser, the other types in
the shell, and the agent narrates in the third pane. Everything is one page.

---

## Architecture

```
                          ┌──────────────────────────────────────┐
   browser (you)  ────────▶│  console  /  (web/console.html+js)   │
                          └───────┬──────────────────┬───────────┘
                                  │ SSE: shell bytes │ SSE: browser frames
                                  ▼                  ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                      agent_linux.service  (:3100)                   │
   │                                                                     │
   │  api.py ─────────── the shell contract  /terminal/pty/*             │
   │  browser_api.py ─── the browser surface /agent/browser/*            │
   │  agentbox.py ────── the agent           /agent/*                    │
   │  extensions.py ──── MCP/skills/plugins  /agent/extensions/*         │
   └───────┬──────────────┬───────────────┬──────────────┬──────────────┘
           │              │               │              │
           ▼              ▼               ▼              ▼
     ┌──────────┐   ┌──────────┐   ┌───────────┐  ┌──────────────┐
     │  pty.py  │   │browser.py│   │ agentbox  │  │   store.py   │
     │ 8 PTYs   │   │ 1 Chromium│  │ tool loop │  │ file|pg|sb   │
     └──────────┘   └──────────┘   └─────┬─────┘  └──────────────┘
                                        │
                    ┌───────────────────┼────────────────────┐
                    ▼                   ▼                    ▼
              ┌──────────┐       ┌───────────┐        ┌───────────┐
              │  mcp.py  │       │ skills.py │        │plugins.py │
              │ MCP srvrs│       │ SKILL.md  │        │ http/py   │
              └──────────┘       └───────────┘        └───────────┘
```

### The `link.py` seam

The one abstraction that makes the package portable. The same shell contract is
served either **in-process** or by **another host**:

```
AGENT_LINUX_URL unset  →  LocalLink    shells live in this process
AGENT_LINUX_URL set    →  RemoteLink   HTTP + SSE to a separately hosted terminal
```

Both expose the same async surface, so `api.py` — and therefore every client —
cannot tell the difference. A remote failure rebuilds into the same exception
locally, because errors cross the seam as stable `code` strings.

### Data flow of one agent turn

```
1. POST /agent/chat  {message, provider?}
2. system prompt assembled:   base rules
                            + skills index        (one line per skill)
                            + database note       (if a DB is connected)
                            + browser note        (if Playwright is present)
3. tool list assembled: built-ins (13) + MCP tools (mcp__* ) + plugins (plugin__*)
4. loop, up to AGENT_LINUX_AGENTBOX_MAX_STEPS:
      model → tool_calls → run_tool() → result → back to the model
5. every shell-shaped tool types into a REAL tab you are watching
6. `finish` or no more tool calls → reply + the full step list
```

---

## Project structure

```
agent_linux/
├── __init__.py          package entry; re-exports the link seam only (cheap imports)
├── service.py           the standalone host — FastAPI app, middleware, routes, /health
├── config.py            every setting, read through env.py
├── env.py               AGENT_LINUX_* with NOVA_* fallback + the security alias map
├── link.py              the seam: LocalLink / RemoteLink, and the error vocabulary
├── api.py               the shell HTTP contract (mounted by both hosts)
├── pty.py               real PTY session manager (the shells behind the tabs)
├── sandbox.py           simulated allowlist executor + `nova` help text
├── agentbox.py          the agent: tools, providers, the tool loop
├── extensions.py        MCP + skills + plugins + database, as one router
├── mcp.py               MCP client: Streamable HTTP and stdio
├── skills.py            SKILL.md / .zip parsing and progressive disclosure
├── plugins.py           declarative HTTP tools + opt-in python plugins
├── store.py             pluggable persistence: file | supabase | postgres
├── secrets.py           the encrypted vault (SSH keys, account credentials)
├── ssh.py               SSH keys, known_hosts, connections, probing
├── accounts.py          named credentials per provider, and the active one
├── credentials.py       the /agent/ssh/* and /agent/accounts/* surface
├── browser.py           the shared Chromium session
├── browser_api.py       the browser's HTTP surface + frame stream
├── web/
│   ├── console.html     the console page (5 themes, drawer, browser pane)
│   └── console.js       its client (SSE, xterm.js, frame renderer, shortcuts)
├── deploy/
│   ├── README.md        which host to pick, and what "free" really costs
│   ├── smoke.sh         one-shot health/contract test for a deployment
│   ├── compose.yaml     your own VPS, with a persistent volume
│   ├── fly.toml         ~$2–3/mo always-on with a volume
│   ├── render.yaml      free, 750 h/mo, sleeps after 15 min
│   └── hf-space/        free, does NOT sleep after 15 min — the best free tier
├── Dockerfile           builds this directory alone
├── requirements.txt     3 required packages, 2 documented optional ones
└── run.sh               launcher for the standalone host
```

**Dependency direction is one-way.** `agent_linux` never imports a host
application. `__init__.py` deliberately does not import `api.py` (FastAPI) or
`sandbox.py`, so importing the seam stays cheap on both sides.

---

## Backend logic

### `pty.py` — real shells

Each `TerminalSession` is a `pty.fork()`; the child execs `bash` with a generated
rcfile so the prompt and MOTD always win over the image's `/root/.bashrc`.

- **Working directory** — the shell emits OSC 7 before every prompt; a reader
  thread parses it, so the tab list shows a real path after `cd`. A 256-byte tail
  is retained across reads so a sequence split between chunks still parses.
- **Scrollback** — 512 KB ring per session, and the cursor is a *total-bytes*
  counter rather than a buffer index, so it stays correct after the ring trims.
- **Lifecycle** — 8-session hard cap (`session_limit`), 30-minute idle reap,
  `SIGHUP` then `SIGKILL` on close, all PTYs torn down at shutdown.

### `agentbox.py` — the tool loop

Three groups of tools, and the routing is by name so an invented tool fails
loudly rather than silently doing nothing:

| Group | Tools |
| --- | --- |
| Shell & files | `run_command`, `read_file`, `write_file`, `list_sessions`, `finish` |
| Knowledge | `read_skill`, `read_skill_file`, `sql` |
| Browser | `browser_open`, `browser_read`, `browser_click`, `browser_type`, `browser_scroll`, `browser_eval`, `browser_screenshot` |
| MCP | `mcp__<server>__<tool>` — every tool of every enabled server |
| Plugins | `plugin__<name>` — one per registered plugin |
| SSH | `ssh_run`, `ssh_hosts` — run on a saved host; list what exists |
| Accounts | `accounts`, `account_env` — list (masked); run with one active |

**Providers** are OpenAI-compatible endpoints, from two sources: the environment
(`id: env`, cannot be deleted) and `POST /agent/providers` (persisted, keys
returned masked). With none configured the agent answers `503
agentbox_unconfigured` and `/health` says `configured: false` — it never calls
out on its own.

### `browser.py` — one Chromium, two drivers

Every action serialises on a single `asyncio.Lock`, so a user's click and the
agent's keystroke can never interleave halfway. Frames are the exception: a
request arriving mid-action returns `204` rather than queueing behind a slow
navigation, because a stale frame beats a laggy live view.

`browser_read` returns the page text **and the interactive elements** with their
text/name/id — that is what lets the model click a button by its label instead of
guessing a selector that does not exist.

### `store.py` — persistence that is optional

| `AGENT_LINUX_STORE_BACKEND` | What it is | `sql` tool | Needs |
| --- | --- | --- | --- |
| `file` *(default)* | JSON under `<workspace>/.nova-store/` | ✗ | nothing |
| `supabase` | PostgREST table | ✗ | project URL + service key |
| `postgres` | DSN — Neon, Supabase, Railway, RDS | ✓ | `pip install asyncpg` |

Documents hold MCP servers, skills and plugins. File writes are
write-then-`os.replace`, so a crash cannot truncate a good document.

### `env.py` — the rename that cannot hurt you

Every setting is `AGENT_LINUX_*`. The legacy `NOVA_*` names are still read, and
the alias map handles suffixes that were *shortened* in the rename:

```python
AGENT_LINUX_PORT   ←  NOVA_TERMINAL_PORT      (alias, still honoured)
AGENT_LINUX_TOKEN  ←  NOVA_TERMINAL_TOKEN     (alias, always honoured)
```

The token is read from both prefixes **unconditionally** and logs a loud warning.
The failure mode being designed against is specific and nasty: an operator
upgrades, their old token is no longer recognised, and the service hands out root
shells to anyone who can reach the port. That cannot happen here.

---

## The HTTP contract

Everything is under one of three prefixes. `X-Nova-Terminal-Token` (or
`Authorization: Bearer …`) is required on all of it except `/health`, `/` and
`/static/console.js`.

### Shell — `/terminal/pty/*`

*This prefix is an API contract and is deliberately unchanged by the rename.*

| Method | Path | Body → Response |
| --- | --- | --- |
| `GET` | `/sessions` | → `{sessions[], active, max_sessions}` |
| `POST` | `/sessions` | `{cwd?, cols?, rows?, label?}` → `{session, info}` |
| `POST` | `/activate` | `{session}` → `{info}` |
| `POST` | `/rename` | `{session, label}` → `{info}` |
| `GET` | `/stream` | `?session=&offset=` → **SSE**: `{o, cwd, done, exit}` |
| `POST` | `/input` | `{session, data}` — raw keystrokes, `\r`, `\u0003`, arrows |
| `POST` | `/resize` | `{session, cols, rows}` |
| `POST` | `/stop` | `{session}` → `{state}` |
| `POST` | `/run` | `{command, label?, timeout?}` → `{output, code}` |

Errors always carry a stable `code`: `session_gone`, `session_limit`,
`session_start_failed`, `session_write_failed`, `link_unavailable`.

### Agent — `/agent/*`

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/chat` | `{message, provider?, model?, history?, label?}` → `{reply, steps[], extensions[]}` |
| `GET` | `/providers` | list (keys masked) |
| `POST` | `/providers` | add/update one |
| `DELETE` | `/providers/{id}` | forget one |
| `GET` | `/models` | `?provider=` → what that endpoint offers |
| `GET` | `/` | the console page |

### Browser — `/agent/browser/*`

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `` | `{running, url, title, viewport, available, counters}` |
| `POST` | `/start` `/stop` `/resize` | lifecycle |
| `POST` | `/navigate` | `{url, wait?}` |
| `POST` | `/action` | `{action: click\|type\|press\|scroll\|back\|goto, …}` |
| `GET` | `/frame` | one JPEG, or `204` while busy |
| `GET` | `/stream` | **SSE**: `frame` (base64 jpeg), `state`, `error` |

> The frame stream accepts the token as `?token=` because `EventSource` cannot
> send headers. It is a read-only stream of a page that token can already drive —
> the usual trade for a live view. Every other route uses the normal header.

### Extensions — `/agent/extensions/*`

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `` | what is installed, and on which store backend |
| `GET` | `/tools` | every extra tool the agent would see |
| `GET` | `/catalogue` | ready-made MCP/skill/plugin examples |
| `GET` `POST` `DELETE` | `/mcp[/{id}]` | MCP servers |
| `POST` | `/mcp/{id}/test` `/toggle` | connect & list tools · enable/disable |
| `GET` `POST` | `/skills` | skills |
| `POST` | `/skills/upload` | multipart `.md` or `.zip` |
| `GET` `PATCH` `DELETE` | `/skills/{name}` | one skill |
| `GET` `POST` | `/plugins` | plugins |
| `POST` | `/plugins/{name}/test` | dry-run with sample args |
| `GET` | `/db` | the store + the setup SQL to paste into Supabase |
| `POST` | `/db/query` | `{sql, params?}` — postgres backends only |

### `GET /health` — deliberately open

```json
{
  "ok": true, "status": "healthy", "service": "agent_linux",
  "uptime_s": 412, "sessions": 1, "active": "pty-…-1", "max_sessions": 8,
  "build_root": "/app/build", "port": 3100, "public_url": null,
  "auth_required": true,
  "agentbox": { "configured": true, "providers": ["env"], "model": "llama-3.3-70b" },
  "store":    { "backend": "file", "table": "nova_docs", "configured": true },
  "browser":  { "playwright": true, "running": false, "enabled": true }
}
```

---

## Connection & configuration

### Both sides of the link

```bash
# ── the terminal host ────────────────────────────────────────────
AGENT_LINUX_TOKEN=<shared secret>       # gates root shells. Set it.
AGENT_LINUX_PORT=3100                   # its own port
AGENT_LINUX_HOST=0.0.0.0                # 127.0.0.1 for a loopback-only box
AGENT_LINUX_BUILD_ROOT=/app/build       # where shells start

# ── the client (your app) ────────────────────────────────────────
AGENT_LINUX_URL=http://terminal-host:3100   # unset = in-process shells
AGENT_LINUX_TOKEN=<same secret>
```

### Agent

```bash
AGENT_LINUX_AGENTBOX_BASE_URL=https://api.groq.com/openai/v1
AGENT_LINUX_AGENTBOX_API_KEY=gsk_…
AGENT_LINUX_AGENTBOX_MODEL=llama-3.3-70b-versatile
AGENT_LINUX_AGENTBOX_MAX_STEPS=8        # tool-call budget per task
AGENT_LINUX_AGENTBOX_STEP_TIMEOUT=120   # seconds per tool call
```

### Full reference

| Variable | Default | What it does |
| --- | --- | --- |
| `AGENT_LINUX_TOKEN` | *(empty)* | Shared secret. **Empty = open root shell.** Only sane on loopback. |
| `AGENT_LINUX_PORT` | `3100` → `$PORT` → `3101` | Bind port. Free hosts inject `$PORT`; that is honoured. |
| `AGENT_LINUX_HOST` | `0.0.0.0` | Bind address. |
| `AGENT_LINUX_PUBLIC_URL` | *(empty)* | Cosmetic; echoed in `/health`. |
| `AGENT_LINUX_URL` | *(empty)* | Client side: reach a terminal hosted elsewhere. |
| `AGENT_LINUX_BUILD_ROOT` | `/app/build` | Workspace. Point at persistent storage. |
| `AGENT_LINUX_AGENTBOX_*` | *(empty)* | Model endpoint, key, model, steps, timeout, provider file. |
| `AGENT_LINUX_STORE_BACKEND` | `file` | `file` · `supabase` · `postgres` |
| `AGENT_LINUX_STORE_URL` | *(empty)* | Project URL or `postgresql://…` DSN. |
| `AGENT_LINUX_STORE_KEY` | *(empty)* | Supabase service key. |
| `AGENT_LINUX_STORE_TABLE` | `nova_docs` | Document table. |
| `AGENT_LINUX_STORE_READONLY` | `0` | Make the `sql` tool SELECT-only. |
| `AGENT_LINUX_MCP_SERVERS` | *(empty)* | JSON array of MCP servers baked in at deploy. |
| `AGENT_LINUX_PLUGINS_ALLOW_CODE` | `0` | Allow python plugins (same trust as the shell). |
| `AGENT_LINUX_SECRET_KEY` | *(empty)* | Vault key for stored SSH keys and account credentials. Unset = a 0600 key file beside the workspace. |

**Legacy names still work.** `NOVA_TERMINAL_TOKEN`, `NOVA_TERMINAL_PORT`,
`NOVA_BUILD_ROOT`, `NOVA_AGENTBOX_*`, `NOVA_STORE_*` and the rest are read with a
deprecation warning. See [Renaming](#renaming-from-novarouter).

---

## Setup

### Local, from source

```bash
git clone <your-repo> && cd agent_linux/..

# The package must be importable as `agent_linux`, so run from its parent.
python3 -m venv .venv && . .venv/bin/activate
pip install -r agent_linux/requirements.txt

# Optional extras
pip install playwright && python3 -m playwright install --with-deps chromium
pip install asyncpg                     # only for the postgres store backend

AGENT_LINUX_TOKEN=$(openssl rand -hex 24) \
AGENT_LINUX_PORT=3100 \
python3 -m agent_linux.service
```

Then open **http://127.0.0.1:3100/** — the console asks for the token once and
keeps it in `localStorage`.

> Use `python3 -m agent_linux.service`, not `python3 agent_linux/service.py`.
> The `-m` form puts the *parent* directory on `sys.path`, which is what the
> `agent_linux.*` imports need. The directory must be named `agent_linux`.

Or let the launcher do it: `sh agent_linux/run.sh`

### Docker

```bash
cd agent_linux
docker build -t agent-linux .
docker run -p 3100:3100 \
  -e AGENT_LINUX_TOKEN=$(openssl rand -hex 24) \
  -e AGENT_LINUX_AGENTBOX_BASE_URL=https://api.groq.com/openai/v1 \
  -e AGENT_LINUX_AGENTBOX_API_KEY=… \
  -v agent_linux_build:/data \
  -e AGENT_LINUX_BUILD_ROOT=/data/build \
  agent-linux
```

### Docker Compose (with a persistent workspace)

```bash
cd agent_linux
AGENT_LINUX_TOKEN=$(openssl rand -hex 24) docker compose -f deploy/compose.yaml up -d
```

### First five minutes

```bash
# 1. Is it alive?
curl -s localhost:3100/health | python3 -m json.tool

# 2. Open a shell and run something
curl -s -H "X-Nova-Terminal-Token: $TOKEN" \
     -H 'content-type: application/json' \
     -d '{"command":"whoami && uname -a"}' \
     localhost:3100/terminal/pty/run

# 3. Add a model provider, then ask the agent something
curl -s -H "X-Nova-Terminal-Token: $TOKEN" -H 'content-type: application/json' \
  -d '{"id":"groq","base_url":"https://api.groq.com/openai/v1",
       "api_key":"gsk_…","model":"llama-3.3-70b-versatile"}' \
  localhost:3100/agent/providers

curl -s -H "X-Nova-Terminal-Token: $TOKEN" -H 'content-type: application/json' \
  -d '{"message":"what is running on this box?"}' \
  localhost:3100/agent/chat

# 4. Verify a deployment end to end
BASE=https://your-host TOKEN=$TOKEN sh deploy/smoke.sh
```

### Giving the agent capabilities

```bash
# An MCP server — its tools join the agent's toolbox automatically
curl -s -H "X-Nova-Terminal-Token: $TOKEN" -H 'content-type: application/json' \
  -d '{"id":"fetch","transport":"stdio","command":"npx",
       "args":["-y","@modelcontextprotocol/server-fetch"]}' \
  localhost:3100/agent/extensions/mcp
curl -s -XPOST -H "X-Nova-Terminal-Token: $TOKEN" \
  localhost:3100/agent/extensions/mcp/fetch/test

# A skill — instructions the agent loads only when a task matches
curl -s -H "X-Nova-Terminal-Token: $TOKEN" \
  -F file=@./release-notes.md localhost:3100/agent/extensions/skills/upload

# A plugin — a declarative HTTP tool, no code executed on the host
curl -s -H "X-Nova-Terminal-Token: $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"notify-slack","description":"Post to Slack",
       "parameters":{"type":"object","properties":{"text":{"type":"string"}},
                     "required":["text"]},
       "request":{"method":"POST","url":"${env.SLACK_WEBHOOK_URL}",
                  "body":{"text":"${text}"}}}' \
  localhost:3100/agent/extensions/plugins
```

Or do all of it in the console: **⧉** in the topbar opens the extensions drawer,
with an **Examples** tab whose buttons fill the forms for you.

### Connecting a database

```bash
# Supabase (REST — no driver to install)
AGENT_LINUX_STORE_BACKEND=supabase
AGENT_LINUX_STORE_URL=https://<project>.supabase.co
AGENT_LINUX_STORE_KEY=<service_role key>
# then paste the setup SQL from: GET /agent/extensions/db

# Or one DSN — which also gives the agent a real `sql` tool
AGENT_LINUX_STORE_BACKEND=postgres
AGENT_LINUX_STORE_URL=postgresql://user:pass@host/db
AGENT_LINUX_STORE_READONLY=1
```

Without either, everything still works — the `file` backend keeps MCP servers,
skills and plugins beside the workspace. A redeploy wipes a container's disk,
which is the one thing a database buys you.

---

## Deploy

`deploy/README.md` is the full decision table (checked against vendor pricing).
The short version: **every free container host spins down when nobody is
looking**, which is the opposite of what a shell needs.

| Pick | Why |
| --- | --- |
| **Hugging Face Spaces** (`cpu-basic`) | 🥇 The only free tier that does **not** sleep after 15 minutes — Spaces pause after 48 h. `/data` survives restarts. → `deploy/hf-space/` |
| **Fly.io** (~$2–3/mo) | 💰 The honest answer once it matters: always-on, persistent volume, `auto_stop_machines = false`. → `deploy/fly.toml` |
| **Render** (free) | 750 h/mo, no card, but 15-minute sleep. → `deploy/render.yaml` |
| **Your own VPS** | Full control, persistent volume. → `deploy/compose.yaml` |
| **Cloud Run / Lambda** | ❌ Wrong shape — request-billed, scales to zero, kills long-lived SSE shells. |

One caveat worth repeating: **Playwright does not fit on a small free tier.**
Chromium plus its dependencies is ~400 MB. If you want the browser on a free
host, use Spaces (`cpu-basic` has the headroom); elsewhere the agent still works
and only the browser is unavailable.

---

## SSH & accounts

Two features that are really one problem: credentials. Both store secrets
**encrypted**, and both obey the same rule — a mask goes out over the API, the
plaintext stays inside the process that needs it.

### SSH — connect to remote hosts from the terminal

An SSH session here is **a real terminal tab running `ssh`**. `pty.py` can exec an
arbitrary `argv` on its PTY, so there is no proxied subprocess and no bespoke UI:
host-key prompts, password prompts, `~/.ssh/config` aliases, port forwarding and
`scp` all work, because it is the actual `ssh` client.

```bash
T="X-Nova-Terminal-Token: $TOKEN"
B=localhost:3100

# 1. Generate a keypair on this host (ed25519). The private half goes to the vault.
curl -s -H "$T" -H 'content-type: application/json' \
  -d '{"name":"prod","comment":"deploy@agent-linux"}' \
  $B/agent/ssh/keys

# 2. Adopt a key you already have (public half is derived if you omit it).
curl -s -H "$T" -H 'content-type: application/json' \
  -d '{"name":"legacy","private_key":"-----BEGIN OPENSSH PRIVATE KEY-----\n…"}' \
  $B/agent/ssh/keys/import

# 3. Save a connection.
curl -s -H "$T" -H 'content-type: application/json' \
  -d '{"name":"web1","hostname":"10.0.0.5","user":"deploy","port":22,
       "auth":"key","key":"prod"}' \
  $B/agent/ssh/hosts

# 4. Does the key actually authenticate? No tab opened, no prompt.
curl -s -XPOST -H "$T" $B/agent/ssh/hosts/web1/probe

# 5. Open it. This creates a real terminal tab you can type into.
curl -s -XPOST -H "$T" $B/agent/ssh/hosts/web1/open
```

What is deliberately **not** here: the agent can run a command on a host, but it
cannot read a private key or a saved password. `ssh_run` builds the same argv the
interactive tab would and hands the secret to that child process only.

| Endpoint | What it does |
| --- | --- |
| `GET /agent/ssh` | keys, hosts, availability, vault status |
| `GET /agent/ssh/keys` | list — **public halves only** |
| `POST /agent/ssh/keys` | generate a keypair on this host |
| `POST /agent/ssh/keys/import` | adopt an existing private key |
| `GET /agent/ssh/keys/{name}/private` | reveal it, deliberately and audibly |
| `GET POST DELETE /agent/ssh/hosts[/{name}]` | saved connections |
| `POST /agent/ssh/hosts/{name}/open` | open a real ssh tab |
| `POST /agent/ssh/hosts/{name}/probe` | non-interactive auth check |
| `GET /agent/ssh/known_hosts` · `DELETE` | what is trusted, and forgetting it |

**Host keys are real.** `StrictHostKeyChecking=accept-new` is the honest middle
ground: an unknown host is accepted and *recorded* the first time, a *changed*
key is refused loudly. `no` would be silent MITM; `yes` would make a fresh host
unusable without a manual step the console cannot perform. `known_hosts` lives in
`&lt;workspace&gt;/.agent_linux-ssh/` and survives restarts.

**Key material never lingers.** A key is written to a 0600 file only for the
moment a connection needs it, and removed when that connection ends — including
when a tab closes. Passwords are supported but never preferred: a saved password
becomes a 0700 askpass script for the duration of the connection, then is deleted,
so it never appears in an argv or a process listing.

### Accounts — many credentials per provider

The problem this solves is mundane and real: a personal and a work Supabase
project, two Cloudflare accounts, three Google service accounts, a Neon branch
database per environment. One global `AGENT_LINUX_STORE_URL` cannot express that,
and pasting a DSN into a shell command is how credentials end up in a history file.

Accounts are **named documents grouped by provider**:

| Provider | Fields |
| --- | --- |
| `postgres` | `url` (DSN) · `sslmode` |
| `neon` | `url` · `branch` |
| `supabase` | `project_url` · `service_key` · `anon_key` · `db_url` |
| `cloudflare` | `account_id` · `api_token` · `zone_id` · `r2_bucket` |
| `google` | `service_account_json` · `project_id` · `bucket` · `region` |
| `github` | `token` · `owner` |
| `generic` | any key/value pair |

```bash
# Add two Cloudflare accounts side by side.
curl -s -H "$T" -H 'content-type: application/json' \
  -d '{"provider":"cloudflare","name":"work",
       "account_id":"…","api_token":"…"}' $B/agent/accounts

curl -s -H "$T" -H 'content-type: application/json' \
  -d '{"provider":"cloudflare","name":"personal",
       "account_id":"…","api_token":"…"}' $B/agent/accounts

# Make one active (only one per provider).
curl -s -XPOST -H "$T" $B/agent/accounts/cloudflare/work/activate

# What variables does it export? Masked by default.
curl -s -H "$T" $B/agent/accounts/cloudflare/work/env
```

**Activating a database account is a real switch.** With a `postgres`, `neon` or
`supabase` account active, the store is rebuilt from that account's DSN — so the
agent's `sql` tool moves to that database immediately, with no restart and no
edited environment. Deactivating it falls back to the environment, then to `file`.

**Secrets are exported, never printed.** A command runs with an account's
credentials in its environment via a 0600 file that it sources and deletes — not
an inline `env KEY=… cmd`, which would leak into the terminal's scrollback and
into `ps`.

### The vault

Both features store through `secrets.py`, which encrypts by field name
(`…_token`, `…_key`, `…_url` are secret) using Fernet.

| Setting | Behaviour |
| --- | --- |
| `AGENT_LINUX_SECRET_KEY` set | Used directly — a Fernet key, or any passphrase (derived with SHA-256). **Do this in production.** |
| unset | A key is generated on first use at `<workspace>/.agent_linux_vault.key`, 0600, and a loud warning is logged. It protects against a database dump, **not** against someone with the disk. |
| `cryptography` missing | Every credential route answers `vault_unavailable` with the install command; nothing else is affected. |

A secret is only ever decrypted for the code that *uses* it. `public()` masks
every secret field rather than dropping it, so the console can show *that* a
token is set without showing which.


---

## Security model

**This service hands out root shells. Treat the token as you would an SSH key.**

| Control | Behaviour |
| --- | --- |
| `AGENT_LINUX_TOKEN` | Gates everything except `/health`, `/` and `/static/console.js`. Compared exactly; a missing token is a `401` with `code: unauthorized`. |
| No token set | Only sane on loopback. The service prints a loud warning block at boot, and `/health` reports `auth_required: false`. |
| Secrets never echoed | Provider keys, MCP headers/env and plugin auth headers are returned **masked** (`sk-l…bcd`). A plugin's code is returned only on a host that already allows it to run. |
| `${env.NAME}` | Plugin and MCP credentials are templated from the deployment's environment, so a token never enters the model's context or a stored document. |
| Python plugins | Off unless `AGENT_LINUX_PLUGINS_ALLOW_CODE=1`. Same trust level as the shell, which is exactly why it is opt-in — an upload must not become RCE by accident. |
| Store keys | Never logged. `/health` reports the backend and table, never the DSN or key. |
| Browser frame stream | The one route that accepts `?token=`, because `EventSource` cannot send a header. Read-only, and a page the token holder can already drive. |
| Idle browser | Closed after 30 minutes (the health poll is the clock) and at shutdown. A forgotten Chromium is 400 MB of resident memory. |

**Hardening checklist for anything public:**

1. Set `AGENT_LINUX_TOKEN` to `openssl rand -hex 24` or longer.
2. Put TLS in front (the platforms in `deploy/` all do).
3. Set `AGENT_LINUX_STORE_READONLY=1` unless the agent genuinely needs to write.
4. Leave `AGENT_LINUX_PLUGINS_ALLOW_CODE` unset.
5. Remember the browser is a *logged-out* profile by design — it has no
   persistent cookies, so it cannot silently act as you.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'agent_linux'` | Run from the **parent** directory with `python3 -m agent_linux.service`, or set `PYTHONPATH=<parent>`. The directory must be named `agent_linux`. |
| Everything answers `401` | The token does not match. It is compared exactly — check for a trailing newline in your shell export. |
| `/agent/extensions` says `store_unavailable` | A DB backend is configured but unreachable. `/agent/extensions/db` reports the error; drop back to `file` to unblock. |
| `agentbox_unconfigured` | No model provider. Set `AGENT_LINUX_AGENTBOX_BASE_URL`/`_API_KEY`, or `POST /agent/providers`. |
| Browser routes answer `browser_unavailable` | Playwright is not installed: `pip install playwright && python3 -m playwright install --with-deps chromium`. |
| Chromium fails to launch | Usually a missing shared library (`--with-deps` installs them) or a read-only `HOME`. |
| Frames never arrive | The stream needs `?token=` — check the browser console for a 401 on `/agent/browser/stream`. |
| Shell routes `404` | You are on a host that mounted the routes elsewhere. This build serves them at `/terminal/pty/*`; `GET /health` confirms the service is the one answering. |
| `sql` says "the file store has no SQL" | Expected. Point `AGENT_LINUX_STORE_BACKEND=postgres` at a DSN to get it. |
| A skill upload is rejected | Needs a `name` — either frontmatter `name:` or a filename that is a valid id (`[a-z0-9._-]`). |
| `vault_unavailable` | `pip install cryptography`. Stored credentials need it; nothing else does. |
| `cannot decrypt with the current vault key` | `AGENT_LINUX_SECRET_KEY` changed, or the vault key file was lost. The stored secret must be re-entered. |
| `ssh: Could not resolve hostname` on a saved host | The hostname is wrong. This build already puts the destination before any remote command, which is the other cause. |
| `host key verification failed` | The server was rebuilt. `DELETE /agent/ssh/known_hosts` with the hostname, then reconnect — but check first, because a changed key is also what a MITM looks like. |
| Activating a DB account does nothing | Check `/agent/extensions/db` for the reason: usually the DSN is unreachable, or `asyncpg` is missing for a postgres account. |
| MCP server `test` times out | stdio servers get 25 s to answer. First run of `npx -y …` downloads the package; retry once it is cached. |

---

## Renaming from NovaRouter

The project was `NovaRouter`'s terminal, published as `terminal/`. It is
**Agent_Linux**, published as `agent_linux/`. What that means for you:

| Changed | Detail |
| --- | --- |
| Package directory | `terminal/` → `agent_linux/` |
| Entry point | `python3 -m terminal.service` → `python3 -m agent_linux.service` |
| Env prefix | `NOVA_*` → `AGENT_LINUX_*` |
| Shell prompt | `~ Root@Build:` → `~ AgentLinux:` |
| Docker image | `novarouter-terminal` → `agent-linux` |

**Unchanged on purpose:**

- **The URL prefix `/terminal/pty/*`.** It is an API contract. Anything already
  pointed at this host — a dashboard, a script, a saved integration — keeps
  working, and the package's own name appearing in a URL would be a leaky
  abstraction anyway.
- **The legacy env names.** `NOVA_TERMINAL_TOKEN` and friends are read forever,
  with a deprecation warning. The token in particular is read from *both*
  prefixes unconditionally, so a rename can never leave the service open.
- **The store table name `nova_docs`.** Renaming it would orphan existing data.

### Migrating

```bash
# 1. Move the directory
git mv terminal agent_linux

# 2. Rename your variables (both work, so you can do this at your leisure)
sed -i 's/NOVA_TERMINAL_/AGENT_LINUX_/g; s/NOVA_AGENTBOX_/AGENT_LINUX_AGENTBOX_/g' .env

# 3. Update your start command
#    python3 -m terminal.service  →  python3 -m agent_linux.service

# 4. Verify
python3 -m agent_linux.service &
curl -s localhost:3100/health | python3 -m json.tool
sh deploy/smoke.sh
```

Check `/health` → `env` for which prefix the process is actually running on, and
watch the boot log for any `… is deprecated …` lines telling you what is left.

---

## License

MIT.
