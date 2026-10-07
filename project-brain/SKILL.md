---
name: project-brain
description: Zero-key project memory + codebase map for Agent_Linux (AgentX). Read this before touching the codebase; ask the built-in Brain instead of re-scanning files. Covers module map, request flows, state locations, fix history, conventions and the roadmap.
when_to_use: Any task on this repository — onboarding, debugging, adding routes/tools, changing Agent X or Agentbox, or when another agent asks how something here works.
---

# Project Brain — Agent_Linux (AgentX)

You are working on **Agent_Linux**, a self-contained AI terminal service:
real root PTY shells + a built-in agent (Agentbox) + a peer-agent mesh
(Agent X). It runs alone with `python3 -m agent_linux.service` and imports
nothing but stdlib + httpx + fastapi.

## The rule (this saves you 30+ minutes every session)

**Never re-scan the codebase to understand it. The Brain already did.**

1. Read `PROJECT_MAP.md` (sections only — use the table of contents in §0).
2. Read `MEMORY.md` — fix history, decisions, lessons, next steps.
3. Then open only the 1–2 files your task touches.

Or skip reading entirely and **ask the built-in Brain** — a deterministic,
zero-key agent embedded in the service (no API key, no model, no network):

```bash
# HTTP (the service answers project questions on /agent/brain):
curl "http://127.0.0.1:3100/agent/brain/ask?q=how+does+key+rotation+work"
curl "http://127.0.0.1:3100/agent/brain/next"          # the roadmap
curl "http://127.0.0.1:3100/agent/brain/map?section=agent+x+mesh"
curl "http://127.0.0.1:3100/agent/brain/memory?section=lessons"
```

The engine is `project_brain.py` (`brain_ask(question)` if you are inside
Python). It retrieves the right PROJECT_MAP/MEMORY sections and a live symbol
index (every def/class/route in the package, with file:line).

## Non-negotiable house rules

- **Stack discipline**: stdlib + httpx + fastapi. No new dependencies.
- **Failure isolation**: catch broad, report in the payload, never 500 the
  caller. A dead peer/provider/skill must never break the chat loop.
- **Secrets**: mask everywhere (`mask_key`/`_mask`); keys go into child env,
  never into logs or responses.
- **State**: JSON files with atomic writes under `<build_root>/.agentx/` for
  the mesh; `.agentbox-providers.json` (0600) for providers.
- **Entrypoints are `-m` modules with selftests**:
  `python3 -m agent_linux.agent_x --selftest`,
  `python3 -m agent_linux.project_brain --selftest`.
- **Adding an Agentbox tool = TWO edits**: the `TOOLS` schema list AND the
  `run_tool()` routing branch. Forgetting the second one fails at runtime.
- **Env vars**: `AGENT_LINUX_*` (legacy `NOVA_*` still honored for the token).

## Quick orientation (10-second version)

| Want to… | Touch |
|----------|-------|
| Add an agent tool | agentbox.py → `TOOLS` + `run_tool()` |
| Change provider fallback | agentbox.py → `_ordered_chain`, `mark_provider_failed` |
| Add an HTTP route | a module's `APIRouter`, mount in service.py |
| Add a mesh behavior | agent_x.py (AgentX class) + agent_x_api.py route |
| Change config | env var in config.py, never hardcode |
| Teach the Brain a new fact | POST /agent/brain/remember or edit MEMORY.md |
| Change how the project is understood | edit PROJECT_MAP.md sections |

## After your task: leave the map better

If you changed structure (new module/route/flow), update the matching
PROJECT_MAP.md section and append a row to MEMORY.md's fix-history table (or
use `POST /agent/brain/remember {"area": "...", "note": "..."}`). The next
agent — human or AI — starts where you stopped, not from zero.

## Full workflow for any non-trivial task

1. **Ask the Brain** what exists for your task area (`/agent/brain/ask`).
2. **Check MEMORY.md** for related fixes/lessons — never regress a fixed bug.
3. **Read only the files** the Brain/Map point at.
4. **Implement** following house rules above.
5. **Selftest** the module you touched (`-m agent_linux.<mod> --selftest`).
6. **Update the Brain**: PROJECT_MAP.md section + MEMORY.md entry.
7. **State the next step** — check `/agent/brain/next` first; if you did one,
   tick it off and add the new one.
