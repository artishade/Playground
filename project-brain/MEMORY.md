# Agent_Linux (AgentX) — Project Memory

> The Brain's journal: decisions, fixes, lessons, next steps. Any AI model
> starting work on this project reads this FIRST (after SKILL.md) so it never
> re-derives context or repeats a fixed bug. Append entries; never delete.

## Current state (as-of 2026-10-08)

- Service: `python3 -m agent_linux.service`, port 3100 (or $PORT), token via
  AGENT_LINUX_TOKEN (x-nova-terminal-token header).
- Agentbox providers: `.agentbox-providers.json` in the workspace; env provider
  id `env` via AGENT_LINUX_AGENTBOX_*.
- Agent X mesh: `<build_root>/.agentx/*.json`; bridges = OpenAI-compatible
  peers; novarouter + openrouter shortcuts in KNOWN_ENDPOINTS.
- **NEW: Project Brain live** — `project_brain.py` + `project-brain/` skill.
  Zero-key Q&A at /agent/brain/ask; wired into Agentbox (`project_ask` tool),
  Agent X message envelopes, and the health report.

## Fix history (do not regress)

| Date | Area | What happened |
|------|------|---------------|
| 2026-10-04 | Dockerfile + web | Entrypoint fixed to `python3 -m agent_linux.service` (was terminal.service → ModuleNotFoundError); console.js credential helpers (cred, loadCredentials, showCredPane) completed for AgentLinuxConsole exports |
| 2026-09-28→10-07 | agent_x.py | agentx/1.0 envelopes hardened; knowledge→lesson, task→ledger flow; selftest passes incl. live example.com fetch |

## Decisions (the "why" log)

- **stdlib + httpx + fastapi only.** The package must stay copy-and-run; every
  dep is a deploy risk. (vault_backup, secrets, ssh all follow this.)
- **Progressive disclosure for skills.** One-line index in the system prompt;
  bodies load on demand. Cost of a big library is ~zero until used.
- **Provider failures cool down, never remove.** A 429-ing provider comes back
  automatically; the chain reorders instead of forgetting.
- **JSON state files with atomic writes** for mesh/identity: human-debuggable,
  survives redeploys in the workspace; store is only for things vault_backup
  guards.
- **The Brain is deterministic.** No model call for project Q&A — it must
  answer even when every provider is down or unconfigured.

## Lessons (hard-won)

- Android-side file tools cannot see proot `/root/...` paths — use the linux
  environment when editing this repo from the phone side.
- `$PORT` on hosts (HF/Render) overrides everything; never hardcode 3100 in
  anything user-facing.
- The token middleware accepts `?token=` for EventSource — if you add SSE
  routes, follow the same pattern.
- When adding an Agentbox tool: BOTH `TOOLS` schema and `run_tool()` routing,
  or it fails loudly at runtime.
- grep/search tools running on the Android side cannot resolve `/root/...`;
  read files directly (read_file / read_file_part) instead.

## Next steps (roadmap)

1. **Console: Brain view** — a small panel in web/console (next to Agent X)
   showing /agent/brain/status, a question box → /agent/brain/ask, and the
   next-steps list. Cheap, high visibility.
2. **Brain learns from fixes** — when Agentbox edits code, log a MEMORY.md
   entry automatically (a `brain_remember` call inside the finish path).
3. **Mesh gossip** — peers exchange Brain lessons on connect (agentx/1.0
   knowledge envelopes already support this; add a ping-time exchange).
4. **Brain retrieval upgrade** — if the map grows past ~50KB, add an optional
   tf-idf section scorer instead of keyword overlap (stdlib math only).
5. **NovaRouter integration check** — /agent/x/peers ping the novarouter
   endpoint after each deploy so bridges never silently rot.

## Open questions

- Should the Brain index `web/console.js` (14k+ lines) section-wise too? Maybe
  only the view-registration pattern + fetch helpers.
- Provider file vs store: should `.agentbox-providers.json` move fully into the
  store? vault_backup already snapshots it; leave as-is for now.

## External context

- Sister project NovaRouter/Novarouter2: multi-provider gateway (see
  PROJECT_MAP.md §8). Its fixes feed this file's fix history too.
- Deploy targets: Dockerfile at repo root; deploy/hf-space/Dockerfile for HF.
