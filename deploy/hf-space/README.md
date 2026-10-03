---
title: Agent_Linux Terminal
emoji: 🖥️
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Root cloud shell + AI agent, in one page.
---

# Agent_Linux Terminal on Hugging Face Spaces

The cheapest way to get a **permanently running** root shell for free: Spaces
on `cpu-basic` are **not** stopped after 15 minutes of idleness — the platform
pauses a Space only after **48 hours** of no activity, and a request wakes it.
That is a different world from Render/SnapDeploy, which sleep in 15 minutes.

## Create it

1. **New Space** → SDK **Docker** → blank template.
2. Copy this package into the Space repo and use `deploy/hf-space/Dockerfile`
   as `Dockerfile` (or point the Space at this repo and set the path).
3. **Settings → Variables and secrets**, add:

   | Name | Kind | Value |
   | --- | --- | --- |
   | `AGENT_LINUX_TOKEN` | secret | `openssl rand -hex 24` — **do this**, a public Space URL with no token is an open root shell |
   | `AGENT_LINUX_BUILD_ROOT` | variable | `/data/build` (see below) |
   | `AGENT_LINUX_AGENTBOX_BASE_URL` | secret | e.g. `https://api.groq.com/openai/v1` |
   | `AGENT_LINUX_AGENTBOX_API_KEY` | secret | your key |
   | `AGENT_LINUX_AGENTBOX_MODEL` | variable | e.g. `llama-3.3-70b-versatile` |

4. Open the Space. The console asks for the token once and keeps it locally.

## What you must know

- **Port**: Spaces routes traffic to `app_port` (7860 here). The service reads
  `$PORT`, so nothing else is needed.
- **Ephemeral disk**: a free Space's filesystem is wiped on every rebuild. The
  `AGENT_LINUX_BUILD_ROOT=/data/build` above points the workspace at `/data`, which
  survives restarts within a Space but *not* a factory rebuild. For anything you
  care about, `git push` it or mount real storage.
- **CPU**: `cpu-basic` is 2 vCPU / 16 GB RAM shared — plenty for shells and
  `pip install`, not for compiling a kernel.
- **Public by default**: a Space is discoverable. The token is not optional
  here, and consider setting the Space to **private** for a personal box.