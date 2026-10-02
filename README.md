# `terminal/` — NovaRouter's terminal, one path, hostable alone

Everything the project does with an interactive shell lives in this directory.
Nothing terminal-related exists outside it, and the rest of the app reaches it
only through `terminal.*` imports:

```python
from terminal import link                                   # the seam (LocalLink/RemoteLink)
from terminal.api import router as terminal_router          # the HTTP contract
from terminal.sandbox import execute_command                # one-shot allowlist exec
```

**This package is self-contained.** It imports nothing from `nova/`, needs no
database, and reads the environment itself — so you can copy this one directory
to another machine and run the whole terminal there, with or without a
NovaRouter gateway pointing at it.

## Layout

| File | Role |
| --- | --- |
| `pty.py` | Real PTY sessions behind the Terminal tabs: max 8 concurrent, OSC 7 cwd tracking, 30-min idle reap, labelled agent tabs |
| `sandbox.py` | Simulated allowlist executor for one-shot commands. Only module that *borrows* the app (command history, `nova …`), and it degrades honestly when the app isn't there |
| `link.py` | The seam. `LocalLink` (shells in this process) / `RemoteLink` (HTTP + SSE to another host), chosen by configuration |
| `api.py` | The HTTP contract — mounted by **both** hosts, so they cannot drift |
| `agentbox.py` | **Agentbox**, the AI agent that ships with the terminal (`/agent/*`) |
| `extensions.py` | The extension surface: MCP servers, skills, plugins, database (`/agent/extensions/*`) |
| `mcp.py` | MCP client — Streamable HTTP + stdio, tool namespacing, per-turn tool bridge |
| `skills.py` | Uploadable `SKILL.md` / `.zip` bundles, with progressive disclosure into the prompt |
| `plugins.py` | Declarative HTTP tools (safe default) and opt-in local Python plugins |
| `store.py` | Pluggable persistence: `file` (default) · `supabase` · `postgres` (Neon/Railway/…) |
| `web/console.html` | **The console page** — shell tabs on one side, the agent on the other. Served at `/` |
| `web/console.js` | Its client: session tabs, SSE stream, xterm.js (with a plain-viewer fallback), chat, providers, themes, shortcuts, extensions drawer |
| `service.py` | The standalone host: `python3 -m terminal.service` |
| `config.py` | Every `NOVA_TERMINAL_*` / `NOVA_AGENTBOX_*` knob |
| `Dockerfile` | Builds this directory **alone** — no gateway, no database, no UI |
| `deploy/` | Per-platform configs + the hosting decision (`deploy/README.md`) and a `smoke.sh` health test |

## Deploy just the terminal

Pick whichever fits your host:

```bash
# Docker (build context = this directory)
docker build -t novarouter-terminal ./terminal
docker run -p 3100:3100 \
  -e NOVA_TERMINAL_TOKEN=<shared secret> \
  -e NOVA_AGENTBOX_BASE_URL=https://api.groq.com/openai/v1 \
  -e NOVA_AGENTBOX_API_KEY=<key> \
  novarouter-terminal

# or straight from source (needs fastapi, uvicorn, httpx)
sh ./terminal/run.sh
# python3 -m terminal.service
```

Then point a NovaRouter app at it, or ignore Nova entirely and use it directly:

```bash
NOVA_TERMINAL_URL=http://terminal-host:3100 \
NOVA_TERMINAL_TOKEN=<same secret> \
python3 main.py
```

### The console

Open `http://<terminal-host>:3100/` and you are in the workspace: session tabs
along the top, a real shell (xterm.js — colour, cursor, `vim`, `top`), and
Agentbox on the right. Click a tab to switch, double-click to rename, `×` to
close, **new tab** for another shell. The terminal host serves this page itself,
so a deployed terminal is usable with nothing else running; if the xterm CDN is
unreachable the page falls back to a plain append-only viewer instead of
rendering nothing.

If the host has a `NOVA_TERMINAL_TOKEN`, the page asks for it once (a browser
navigation cannot send a header) and keeps it locally; every API call after that
carries it. `/health` and the page itself stay open — they leak nothing.

`NOVA_TERMINAL_URL` unset (the default) means the app keeps the terminal
in-process and none of this is run — same shells, same routes, one process.

### Where to host it, and what "free" really buys you

`deploy/README.md` is the decision table (checked against vendor pricing in
Sep–Oct 2026), not a link dump. The short version: **every free container host
spins down when nobody is looking**, which is the opposite of what a shell
needs. Two configs are ready to go:

```bash
# Free, and the only free tier that does not sleep after 15 minutes.
# cpu-basic Spaces pause after 48h idle, and /data survives restarts.
#   → deploy/hf-space/README.md, deploy/hf-space/Dockerfile

# ~$2–3/mo, always-on, persistent volume, auto_stop_machines = false.
#   → deploy/fly.toml
```

`deploy/render.yaml` (free, 750 h/mo, 15-min sleep) and `deploy/compose.yaml`
(your own VPS) cover the middle ground. Whatever you pick, verify it in one shot:

```bash
BASE=https://your-host TOKEN=<secret> sh deploy/smoke.sh
```

The image adapts to its host rather than the other way round: it binds
`NOVA_TERMINAL_PORT`, then `$PORT`, then 3100 (`NOVA_TERMINAL_HOST` defaults to
`0.0.0.0`, set `127.0.0.1` for a loopback-only box), and `NOVA_BUILD_ROOT` should
point at the host's persistent path so a redeploy does not wipe your workspace.

| Variable | Where | What it does |
| --- | --- | --- |
| `NOVA_TERMINAL_PORT` | host | Its own port. Precedence: `NOVA_TERMINAL_PORT` → `$PORT` (injected by most free hosts) → `3100`, never the app's |
| `NOVA_TERMINAL_HOST` | host | Bind address, default `0.0.0.0`; `127.0.0.1` for a loopback-only deploy |
| `NOVA_TERMINAL_URL` | app | Set it to use a separately hosted terminal |
| `NOVA_TERMINAL_TOKEN` | **both** | Shared secret for `X-Nova-Terminal-Token`; gates real root shells |
| `NOVA_TERMINAL_PUBLIC_URL` | host | Cosmetic; echoed in `/health` so you can tell which deployment answered |
| `NOVA_BUILD_ROOT` | host | Where shells start (default `/app/build`; point it at the host's persistent path) |
| `NOVA_AGENTBOX_BASE_URL` | host | Any OpenAI-compatible `/v1` endpoint — the agent's brain |
| `NOVA_AGENTBOX_API_KEY` | host | Its API key |
| `NOVA_AGENTBOX_MODEL` | host | Model id (default: first the endpoint offers) |
| `NOVA_AGENTBOX_PROVIDER_FILE` | host | Where saved custom providers live (default: `<workspace>/.agentbox-providers.json`) |
| `NOVA_AGENTBOX_MAX_STEPS` | host | Step budget per task (default 8) |

Notes:
- Use `python3 -m terminal.service`, not `python3 terminal/service.py`: the
  module form puts this directory's parent on `sys.path`, which `terminal.*`
  imports need. The directory must be named `terminal`.
- `/health` and the Agentbox page are open; everything else needs the token
  (`X-Nova-Terminal-Token` or `Authorization: Bearer …`). The page asks for it
  once and keeps it locally. An unset token is only sane on loopback and is
  called out loudly at boot.

## Routes (identical on both hosts)

`GET/POST /sessions`, `POST /activate`, `POST /rename`, `GET /stream` (SSE),
`POST /input`, `POST /resize`, `POST /stop`, `POST /run` — mounted by the
gateway at `/api/admin/terminal/pty/*` and by this host at `/terminal/pty/*`.

Errors always carry a stable `code` (`session_gone`, `session_limit`,
`session_start_failed`, `session_write_failed`, `link_unavailable`) so a remote
failure reads exactly like a local one.

## Agentbox

A root prompt is only half a product, so the terminal host ships the agent too:

```
POST   /agent/chat                 {message, provider?, model?, history?} → {reply, steps[]}
GET    /agent/providers            list custom providers (keys masked)
POST   /agent/providers            add or update one
DELETE /agent/providers/{id}       forget one
GET    /agent/models?provider=id   what that provider can think with
GET    /agent                      the page — shells on one side, agent on the other
```

Its tools (`run_command`, `read_file`, `write_file`, `list_sessions`, `finish`)
all run **in a real terminal tab you can watch**, so a task is visible while it
happens and its shell state survives between steps.

### Custom providers

Any OpenAI-compatible `/v1` endpoint can back the agent — Groq, OpenRouter,
OpenAI, a local vLLM/Ollama, or a NovaRouter gateway's own `/v1`. Add as many
as you like, side by side, and pick one per message:

```bash
curl -XPOST http://terminal-host:3100/agent/providers \
  -H 'X-Nova-Terminal-Token: <secret>' -H 'content-type: application/json' \
  -d '{"id":"groq","label":"Groq","base_url":"https://api.groq.com/openai/v1",
       "api_key":"…","model":"llama-3.3-70b"}'

curl -XPOST http://terminal-host:3100/agent/chat \
  -H 'X-Nova-Terminal-Token: <secret>' -H 'content-type: application/json' \
  -d '{"message":"tail the deploy log","provider":"groq"}'
```

- Saved to `<workspace>/.agentbox-providers.json`, mode `0600`. Override the
  path with `NOVA_AGENTBOX_PROVIDER_FILE`.
- An API key is **only ever echoed back masked** (`dem…456`), so the page and
  `GET /agent/providers` can show which key is set without leaking it. Editing
  a provider with a blank key keeps the stored one.
- `NOVA_AGENTBOX_BASE_URL` / `_API_KEY` / `_MODEL` register one provider called
  `env`, and it wins by default — a deployer can bake in the endpoint while the
  user still adds others. It cannot be deleted over the API; unset the
  variables instead.
- The page has the same controls under **providers**: add, switch, remove.
- With no provider at all the agent stays off and says so: `503` with
  `code: agentbox_unconfigured`, and `/health` reports
  `agentbox.configured: false`. It never calls out on its own.

## Extensions — MCP servers, skills, plugins, and a database

The agent's built-in tools are five shell-shaped verbs plus `read_skill`,
`read_skill_file` and `sql`. Everything else it can do is added here, through one
router (`/agent/extensions/*`) and one console drawer (**⧉** in the topbar).

### MCP servers

Connect Model Context Protocol servers and their tools join the agent's toolbox
automatically. Two transports:

```bash
# Remote (Streamable HTTP). Secrets come from the host environment:
curl -XPOST http://host:3100/agent/extensions/mcp \
  -H 'X-Nova-Terminal-Token: <secret>' -H 'content-type: application/json' \
  -d '{"id":"remote","url":"https://mcp.example.com/mcp",
       "headers":{"authorization":"Bearer ${env.MCP_TOKEN}"}}'

# Local (stdio) — the usual `npx` servers:
curl -XPOST http://host:3100/agent/extensions/mcp \
  -H 'X-Nova-Terminal-Token: <secret>' -H 'content-type: application/json' \
  -d '{"id":"fetch","transport":"stdio","command":"npx",
       "args":["-y","@modelcontextprotocol/server-fetch"]}'

curl -XPOST http://host:3100/agent/extensions/mcp/fetch/test   # connect + list tools
```

- Tools are namespaced `mcp__<server>__<tool>` before the model sees them, so two
  servers can both expose `search` — and a tool call routes back by parsing the name.
- `tools/list` is cached for 5 minutes; the agent does not pay a round trip per message.
- **One broken server never breaks the agent.** It is reported in the reply's
  `extensions` field and on the console, and skipped.
- `NOVA_MCP_SERVERS` bakes servers in at deploy time (JSON array); those are
  read-only over the API.

### Skills

Upload a `.md` with frontmatter, or a `.zip` bundle containing a `SKILL.md`
plus any files it references:

```
POST /agent/extensions/skills/upload        multipart, field `file`
```

```markdown
---
name: release-notes
description: Turn a git log into user-facing release notes.
when_to_use: When the user asks for a changelog.
---

1. Run `git log --oneline <last-tag>..HEAD`.
2. Group commits into Added / Changed / Fixed.
```

Skills use **progressive disclosure**: the system prompt carries only a one-line
index per skill, and the agent calls `read_skill` when a task matches. A library
of twenty skills costs twenty lines of context, not twenty documents. Bundle
files are readable with `read_skill_file`.

### Plugins

A declarative plugin is a name, an argument schema, and a request — **the host
runs no third-party code**:

```bash
curl -XPOST http://host:3100/agent/extensions/plugins \
  -H 'X-Nova-Terminal-Token: <secret>' -H 'content-type: application/json' \
  -d '{"name":"notify-slack","description":"Post to Slack",
       "parameters":{"type":"object",
                     "properties":{"text":{"type":"string"}},"required":["text"]},
       "request":{"method":"POST","url":"${env.SLACK_WEBHOOK_URL}",
                  "body":{"text":"${text}"}}}'
```

`${arg}` comes from the model's tool call, `${env.NAME}` from the host
environment — so a token lives in the deployment and never in the model's
context. A plugin that needs real code is a `kind: "python"` plugin with a
`run(args)` function, enabled only with `NOVA_PLUGINS_ALLOW_CODE=1` (same trust
level as the shell, which is why it is opt-in).

### The database — `file` by default, Supabase/Neon when you want it

Skills, MCP servers and plugins are stored through one pluggable backend, so
`terminal/` still needs **no database** out of the box:

| `NOVA_STORE_BACKEND` | What it is | SQL tool | Needs |
| --- | --- | --- | --- |
| `file` *(default)* | JSON under `<workspace>/.nova-store/` | ✗ | nothing |
| `supabase` | PostgREST, table `nova_docs` | ✗ | project URL + service key |
| `postgres` | DSN — Neon, Supabase, Railway, RDS | ✓ | `pip install asyncpg` |

```bash
NOVA_STORE_BACKEND=supabase
NOVA_STORE_URL=https://<project>.supabase.co
NOVA_STORE_KEY=<service_role key>

# or one DSN, which also gives the agent a real `sql` tool:
NOVA_STORE_BACKEND=postgres
NOVA_STORE_URL=postgresql://user:pass@host/db
NOVA_STORE_READONLY=1     # optional: the sql tool becomes SELECT-only
```

The table is created on first use by the postgres backend; for Supabase REST,
`GET /agent/extensions/db` returns the exact setup SQL to paste into the editor.
A redeploy wipes a container's disk — a database is how your skills and servers
survive it.

### The extension API, in one place

```
GET    /agent/extensions                 what is installed, and on which backend
GET    /agent/extensions/tools           every extra tool the agent would see
GET    /agent/extensions/catalogue       ready-made examples for every section

GET/POST/DELETE /agent/extensions/mcp[/{id}]
POST   /agent/extensions/mcp/{id}/test | /toggle

GET/POST /agent/extensions/skills        PATCH/DELETE /agent/extensions/skills/{name}
POST   /agent/extensions/skills/upload   (.md or .zip)

GET/POST /agent/extensions/plugins       PATCH/DELETE /agent/extensions/plugins/{name}
POST   /agent/extensions/plugins/upload  (.json, or .py on a code-enabled host)
POST   /agent/extensions/plugins/{name}/test

GET    /agent/extensions/db              the store + the setup SQL
POST   /agent/extensions/db/query        one statement (postgres backends)
```

Nothing in this surface ever echoes a secret: API keys, tokens and auth headers
come back masked, and a plugin's code is returned only on a host that already
allows it to run.