# AGENT X — implemented in agent_linux

## What it is
Peer-agent mesh layer for the Agent_Linux terminal. The main agent (Agentbox)
can now talk to other agents and remote models, delegate tasks, visit the web
by itself, and self-improve through a persistent "mind".

Committed: 2a71e98 "feat: Agent X - peer-agent mesh, model bridges, self
online-visit, self-improvement"

## Architecture
- agent_x.py       — engine. Identity, Peer registry (roles: peer|bridge|sub),
                     Mesh store (JSON under <workspace>/.agentx/), bridge calls
                     (OpenAI-compatible /chat/completions), P2P envelopes
                     (agentx/1.0 protocol: message|task|knowledge), online_visit
                     (live browser -> httpx fallback -> readability), mind
                     (turns/tasks/lessons), self_improve reflection loop,
                     selftest (python3 -m agent_linux.agent_x --selftest).
- agent_x_api.py   — 16 routes mounted at /agent/x/* in service.py:
                     status, info, receive, peers(+ping/delete), talk, visit,
                     delegate, send, inbox(+read), mind, lesson, self_improve,
                     prompt.
- service.py       — router mount + agent_x section in /health.
- agentbox.py      — Agent X prompt block injected into _system_prompt(), so
                     the main agent always knows its mesh capabilities.
- web/console.*    — "Agent X" view (Ctrl+6): bridges CRUD, peers CRUD + ping,
                     self online visit, talk-to-bridge, mind viewer, inbox,
                     self-improve button.

## Peer transport paths
- GET  {peer}/agent/x/info      handshake
- POST {peer}/agent/x/receive   envelope delivery (agentx/1.0 protocol header)

## Bridge = how Claude/GPT/GLM/Kimi etc. are reached
Any OpenAI-compatible /v1 endpoint registered as a peer of role=bridge with
model id + api key. talk_to_bridge keeps per-peer conversation memory in the
mind. Known endpoint shortcuts: novarouter, openrouter.

## State
<workspace>/.agentx/{identity,peers,inbox,outbox,mind}.json — survives redeploys.

## Verified
- selftest passes (mesh, mind, envelopes, helpers, live fetch of example.com)
- loopback P2P: self-ping alive, knowledge envelope delivered, lesson stored
- /health reports agent_x section; server log clean
- openapi shows all 16 /agent/x paths; console serves the Agent X view

## Next possible upgrades
- wait=True task execution on peers via their own Agentbox /agent/chat
- bridge auto-discovery sweep (periodic online_visit of router catalogs)
- mesh gossip: peers exchange lessons on connect
- e2e token auth on /agent/x/receive using Identity.secret
