# Agent_Linux (AgentX) — Codebase Map

> Maintained by the Project Brain. Read the section you need — never re-scan
> the whole repository. As-of: 2026-10-08.

## 0. How to use this map

- Fresh session on this project? Read `SKILL.md`, then THIS file, then jump
  straight to the 1–2 files a task touches. That is the whole onboarding.
- Zero-read option: `GET /agent/brain/ask?q=...` answers from this map +
  a live symbol index, with no API key and no model.
- After any structural change (new module, new route, changed flow), update
  the matching section here and log it in `MEMORY.md`.

## 1. Big picture

Agent_Linux is a **self-contained AI terminal service**: real root PTY shells
plus a built-in AI agent (Agentbox) plus a peer-agent mesh (Agent X). It
imports nothing from any host application — stdlib + httpx + fastapi only —
so the `agent_linux/` folder can be copied to any machine and run alone:

    python3 -m agent_linux.service        # binds 0.0.0.0:$PORT (default 3100)

Deployments: `Dockerfile` (repo root) and `deploy/hf-space/Dockerfile`.
The console UI lives in `web/console.*` and is served by the host at `/`.

Three layers, deliberately separated:

| Layer    | Where            | What it does |
|----------|------------------|--------------|
| Terminal | link.py, pty.py  | Real shell sessions/tabs, local or remote-hosted |
| Agentbox | agentbox.py      | The AI agent: providers, tools, fallback chain |
| Agent X  | agent_x.py(+api) | Peer mesh, model bridges, mind, self-improvement |
| Brain    | project_brain.py | Zero-key Q&A over this codebase (this skill) |

## 2. Module map

| File | Role | Key symbols |
|------|------|-------------|
| service.py | FastAPI host: mounts every router, token middleware, /health, serves console | `app`, `require_token`, `lifespan` |
| config.py | All env-driven config; `build_root()` = workspace (/app/build) | `build_root`, `AGENTBOX_*`, `TERMINAL_*` |
| env.py | Env parsing helpers (get/secret/flag/int_or), AGENT_LINUX_* + legacy NOVA_* | `env.get`, `env.secret` |
| link.py | Terminal link abstraction: LocalLink vs remote proxy; `current()` | `pin`, `LocalLink` |
| pty.py | Real PTY sessions; run_command into a labelled tab | `agent_label` |
| agentbox.py | The agent: provider registry, ranked fallback chain w/ cooldowns, TOOLS, run_tool, /agent/chat(+stream) | `Provider`, `run_tool`, `_agent_run`, `_system_prompt` |
| agent_x.py | Mesh engine: Identity, Peer, Mesh (JSON store), bridges, envelopes, online_visit, mind, self_improve | `AgentX`, `Mesh`, `get_agent_x` |
| agent_x_api.py | 16 routes at /agent/x/* for console + peers | `router` |
| skills.py | Skill registry (store-backed), progressive disclosure | `index_prompt`, `read`, `from_zip` |
| extensions.py | MCP servers, skills, plugins HTTP API | `router` |
| mcp.py | MCP client; tool names `mcp__<server>__<tool>` | `parse_tool_name`, `call_tool` |
| plugins.py | Python plugins; tool names `plugin__<name>` | `parse_tool_name` |
| store.py | Document store: file \| supabase \| postgres; `sql` tool backend | `get_store`, `set_active_account` |
| accounts.py / credentials.py / secrets.py | Cloud accounts (postgres/supabase/neon/cloudflare/google/github) + encrypted secrets | `env_for`, `store_config` |
| ssh.py | SSH hosts/keys; agent gets `ssh_run` but never sees keys | `connection_argv`, `_materialise_key` |
| browser.py / browser_api.py | Shared Playwright Chromium the user watches; browser_* tools | `SESSION` |
| vault_backup.py | Auto-snapshot config to the store after mutations; restore on boot | `install_persistence`, `restore_if_needed` |
| api.py | PTY routes for the gateway contract (/terminal/pty) | `router` |
| project_brain.py | Zero-key codebase Q&A + routes at /agent/brain/* | `brain_ask`, `router` |
| web/console.* | Console UI: tabs, agent panel, Agent X view (Ctrl+6) | — |

## 3. Request flows

### 3.1 Agent chat (the core loop)
POST /agent/chat → `agentbox_configured_response()` (503 if no provider) →
`_agent_run()`: resolve provider → privacy check → ranked chain (`_ordered_chain`,
cooldowns push failed providers to the back) → `_chat()` POST
{base}/chat/completions with `TOOLS + extra_tools` → `_tool_calls()` parse →
`run_tool()` per call (shell tools run in a REAL PTY tab via `current()`) →
results appended as `role:"tool"` messages → loop until no calls or `finish`.
Events: start / provider_start / step / fallback / done|error. SSE variant:
/agent/chat/stream.

### 3.2 Skills (progressive disclosure)
System prompt only carries a one-line-per-skill index (`skills.index_prompt`).
The agent calls `read_skill` / `read_skill_file` tools on demand. Uploads:
`.md` or `.zip` with SKILL.md anywhere inside; stored via `store.py` so a
redeploy (or DB backend) keeps them.

### 3.3 Agent X mesh
State: JSON under `<build_root>/.agentx/` — identity, peers, inbox, outbox,
mind (turns/tasks/lessons). Bridges = peers with role "bridge": any
OpenAI-compatible /v1 + model + key; `_bridge_call()` is plain httpx, with
per-peer conversation memory (`history_for`). P2P: POST {peer}/agent/x/receive
with `agentx/1.0` envelopes (message|task|knowledge); knowledge envelopes
become lessons; tasks land in the ledger. `online_visit()`: live browser →
httpx → strip-tags, then a bridge model summarises → lesson saved; can even
register new bridge peers it discovers. `self_improve()`: mind digest →
bridge reflection → new lessons.

### 3.4 The Brain (zero-key)
`project_brain.py` indexes `project-brain/PROJECT_MAP.md` sections +
`MEMORY.md` + a regex symbol sweep of `agent_linux/*.py` (defs, classes,
routes, env vars). `ask()` scores sections by keyword overlap, matches
symbols by name, returns an answer + file pointers. No model, no network.
Questions arriving through `/agent/x/receive` (kind=message) are answered in
the ack by the Brain.

### 3.5 Config persistence
`vault_backup.install_persistence(app)` middleware snapshots providers, MCP
servers, skills, plugins, accounts, SSH material into the store after every
config mutation; on boot `restore_if_needed()` merges them back. A redeploy
cannot take your config.

## 4. State & data locations

| Path | What |
|------|------|
| `<build_root>` (/app/build) | The workspace: shells start here, agent writes here |
| `<build_root>/.agentx/*.json` | Mesh identity, peers, inbox, outbox, mind |
| `<build_root>/.agentbox-providers.json` | Saved model providers (0600, has keys) |
| `<build_root>/.agentbox-system-prompt` | Optional persona override file |
| store (`nova_docs` table / file dir) | Skills, config snapshots, docs |

## 5. HTTP surface (host)

- `/` `/console` — console UI; `/static/console.js`
- `/health` `/api/health` — liveness incl. agentbox + agent_x + brain status
- `/terminal/pty/*` — shell sessions (gateway contract)
- `/agent/chat`, `/agent/chat/stream`, `/agent/providers`, `/agent/models`,
  `/agent/images/generate`, `/agent/local-engines` — Agentbox
- `/agent/extensions/*` — MCP / skills / plugins management
- `/agent/browser/*` — live browser frame stream + controls
- `/agent/x/*` — Agent X: status, info, receive, peers(+ping), talk, visit,
  delegate, send, inbox, mind, lesson, self_improve, prompt
- `/agent/brain/*` — Brain: health, status, map, memory, remember, ask, next
- `/agent/backup*` — vault snapshot/restore

## 6. Conventions (house rules)

- Failure-isolated: a dead peer/model/skill never breaks the chat loop; catch
  broad, report in the payload, never 500 the caller.
- Secrets are masked everywhere (`mask_key`, `_mask`); keys are env-exported
  into child processes, never printed.
- Atomic JSON writes (`_write_json` tmp+replace); thread/asyncio locks around
  shared state.
- New deps only if unavoidable: stdlib + httpx + fastapi is the stack.
- Entrypoints are `-m` modules with selftests:
  `python3 -m agent_linux.agent_x --selftest`,
  `python3 -m agent_linux.project_brain --selftest`.
- Env vars: `AGENT_LINUX_*` (legacy `NOVA_*` still read for token).

## 7. Environment variables (the ones that matter)

| Var | Meaning |
|-----|---------|
| AGENT_LINUX_PORT / HOST | service bind (default 0.0.0.0:3100; $PORT wins) |
| AGENT_LINUX_TOKEN | shared secret gating shells/API (set on BOTH sides) |
| AGENT_LINUX_BUILD_ROOT | workspace root (default /app/build) |
| AGENT_LINUX_AGENTBOX_BASE_URL/_API_KEY/_MODEL | baked-in provider (id `env`) |
| AGENT_LINUX_AGENTBOX_MAX_STEPS / _STEP_TIMEOUT | tool budget / per-call timeout (0 = unlimited) |
| AGENT_LINUX_AGENTBOX_SYSTEM_PROMPT | persona override |
| AGENT_LINUX_PRIVACY_MODE | local-endpoints-only; prompts never leave the host |
| AGENT_LINUX_STORE_BACKEND/_URL/_KEY/_TABLE | file \| supabase \| postgres |
| AGENT_LINUX_MCP_SERVERS | JSON array of MCP servers baked in |

## 8. Sister project: NovaRouter / Novarouter2

FastAPI multi-provider AI gateway (OpenAI-compatible fan-out, key rotation,
cooldowns, model checker). Lives at `/root/Novarouter2` (server) and
`/sdcard/AI Skills/novarouter` (original). Modules: app/{main,gateway,store,
adapters,checker,admin,db,config}.py; SQLite tables providers / upstream_keys
/ client_keys / models / request_log; failover: healthiest provider first, then
round-robin keys; RETRYABLE = {408,409,425,429,500,502,503,504,522,524,402,401,403}.
Agent X registers routers like this as bridge peers (KNOWN_ENDPOINTS has
novarouter + openrouter shortcuts).

Known fixes there (do not regress): XML-regex quoting bugs in agent.py,
Claude streaming timeouts raised to 120s/60s in gateway.py, thinking-block
stripping to avoid "Invalid signature" 400s, gateway request-logging
middleware, missing `periodic_maintenance` import in main.py (NameError at
boot), .gitignore for __pycache__/venv to cut commit size.

## 9. Gotchas

- Dockerfile entrypoint MUST be `python3 -m agent_linux.service` — the old
  `terminal.service` form breaks with ModuleNotFoundError.
- `$PORT` is claimed by hosts; the service never reuses APP_PORT (3000).
- The token middleware also accepts `?token=` (EventSource/img cannot send
  headers) — same secret, weaker channel.
- A saved provider with id `env` shadows the environment provider by design.
- When editing agentbox tools: add to TOOLS **and** route it in `run_tool` —
  unknown names fail loudly by design.
- Android-side file tools cannot see proot paths; use the linux environment
  for `/root/...` reads/greps.
