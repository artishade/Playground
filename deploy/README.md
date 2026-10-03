# Hosting the terminal — free, cheap, and always-on

The terminal is one Docker image with three Python dependencies. That means it
runs almost anywhere, which is a problem: "anywhere" is 14 platforms with 14
different ways of lying about the word *free*. This file is the decision, not a
catalogue.

> Prices and limits below were checked against vendor pricing/docs in
> **Sep–Oct 2026**. Free tiers change without notice — re-verify before you bet
> a project on one.

## First, the uncomfortable truth

A root shell is the most resource-hungry thing you can ask a free tier to host:
it never goes idle (a shell that is *not* running is not a shell), it holds a
process per tab, and it wants a filesystem that survives. Every genuinely free
container host is built on the opposite assumption — *spin down when nobody is
looking*.

So the question is not "which free host?" It is: **do you want free, or do you
want a shell that is there when you come back?** Those are different products.

## The shortlist, scored for *this* workload

| Platform | Free? | Sleeps | Card? | Survives restarts | Verdict for a terminal |
| --- | --- | --- | --- | --- | --- |
| **Hugging Face Spaces** (`cpu-basic`) | yes | only after **48 h** idle | no | `/data` survives restarts, not rebuilds | 🥇 **best free option** — the only free tier that behaves like a real box |
| **Render** | yes, 750 h/mo per workspace | 15 min idle, ~60 s cold start | no | no persistent disk on free | 🥈 fine for a demo; a pinger eats ~720/750 h |
| **Railway** free | $5 trial, then ~$1/mo credit | opt-in | no | volume = paid | 🥉 viable if your usage is genuinely tiny |
| **SnapDeploy** | 100 h/mo, 10 deploys/day | 15 min idle | no | no | only if you specifically want AWS Fargate |
| **Back4App Containers** | 256 MB, 0.25 CPU | stops 60 min after each deploy | no | no | too small to install anything |
| **Koyeb** | 1 service, 0.1 vCPU | scales to zero after 1 h | **yes** ($29 hold) | — | 0.1 vCPU is below usable for a shell |
| **Google Cloud Run / Azure Container Apps** | 180k vCPU-s + 2M req/mo | scales to zero | **yes** | no | request-billing kills a *long-lived* shell — wrong shape entirely |
| **Oracle Cloud Always Free** | 4 ARM OCPU / 24 GB | **idle reclaim** | yes | yes (block volume) | the biggest box, the most ways to lose it |
| **Fly.io** | no (trial only) | you choose | yes | yes (volume) | 💰 the honest answer: **~$2–3/mo for always-on** |
| **Your own VPS + Coolify/Dokku** | software free | never | — | yes | ~$5/mo, full control |

### Why Hugging Face Spaces wins the free bracket

Two facts decide it:

1. **It does not sleep after 15 minutes.** A `cpu-basic` Space is paused only
   after **48 hours** of inactivity. Every other free container host in the
   table spins down in 15–60 minutes. For a terminal, that difference is the
   whole product.
2. **It has a writable mount** (`/data`) that survives restarts, so
   `apt install`-ed tools and your workspace are still there tomorrow.

The cost is that a Space is **public by default**. On a root shell that is not a
nuisance, it is a security incident — so `AGENT_LINUX_TOKEN` is mandatory
there, and setting the Space private is the better move.

→ `deploy/hf-space/README.md` + `deploy/hf-space/Dockerfile`

### Why Render is the runner-up

750 instance hours/month, no card, Dockerfile support, real health checks — a
genuinely good free tier. It is second only because of the 15-minute sleep and
because the 750 hours are **per workspace**, so a keep-warm pinger spends ~720
of them holding one service awake. Great for a demo you show people; annoying as
a daily driver.

→ `deploy/render.yaml`

### Why the serverless ones are wrong for this

Cloud Run, Azure Container Apps and Lambda bill *per request* and scale to zero.
A terminal is the opposite of a request: one SSE stream per tab, open for hours.
You would be billed for idle time you cannot avoid, and every cold start would
kill your shells. Use them for an API, not for this.

### Why Oracle is tempting and why I would not

4 ARM OCPUs and 24 GB RAM, free forever, is the best hardware on this page by an
order of magnitude. The catches are real though: signup approval is regional and
often refused, ARM64 breaks some tooling, there are no automated backups, and
**idle reclamation** can take the instance away when CPU/network use is low —
which is exactly what a shell looks like when you are asleep. It is a great free
server and a mediocre free *terminal*.

## What I would actually do

1. **Today, for free** → deploy to **Hugging Face Spaces**, private, with a
   token and `/data` as the workspace. You get a shell that is still running
   tomorrow, for $0.
2. **The moment it matters** (real work, data you would cry over) → move to
   **Fly.io with a 1 GB volume, `auto_stop_machines = false`**, ~$2–3/month.
   That is cheaper than one coffee and it removes every sleep/reclaim footnote
   from this file. `deploy/fly.toml` is ready.
3. **Skip** Cloud Run/Lambda for this. **Skip** Oracle unless you enjoy
   fighting a platform for a free box.

## The two knobs that make any host work

The image adapts to its host instead of the other way round:

| Variable | Why |
| --- | --- |
| `PORT` / `AGENT_LINUX_PORT` | free hosts inject `$PORT` and route the public URL at it; the service prefers an explicit `AGENT_LINUX_PORT`, then `$PORT`, then 3100 |
| `AGENT_LINUX_HOST` | `0.0.0.0` (default) for a hosted box; `127.0.0.1` for a loopback-only deploy behind your own proxy |
| `AGENT_LINUX_BUILD_ROOT` | point it at the host's persistent path (`/data/build` on Spaces, `/data/build` on Fly with a volume) so the workspace is not wiped on redeploy |
| `AGENT_LINUX_PUBLIC_URL` | cosmetic; echoed in `/health` so you can tell which deployment answered |

`/health` is the probe everything can use, and it is deliberately open:

```bash
curl -s https://your-host/health | jq
# { ok, status, uptime_s, sessions, port, public_url, auth_required, agentbox: {...} }
```

## Always: the token

The service hands out **root shells**. `AGENT_LINUX_TOKEN` is the line between
a private box and a public one, and an unset token is only sane on loopback — the
service shouts about it at boot for exactly this reason. Generate one and set it
on both sides:

```bash
openssl rand -hex 24
# host:  AGENT_LINUX_TOKEN=<value>
# app:   AGENT_LINUX_URL=https://your-host   AGENT_LINUX_TOKEN=<value>
```

The console asks for it once and keeps it in `localStorage`; every API call
after that carries it. `/health` and the page itself stay open because they leak
nothing.