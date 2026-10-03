# syntax=docker/dockerfile:1
# ============================================================================ #
# Agent_Linux Terminal — standalone image.
#
# Build context is THIS directory (the `terminal/` package). The image contains
# the package and three Python dependencies — no gateway, no database, no
# dashboard. Just the shells, their HTTP contract, and the Agentbox agent.
#
#   docker build -t agent-linux .
#   docker run -p 3100:3100 -e AGENT_LINUX_TOKEN=<secret> agent-linux
#
# Cloud hosts that inject $PORT (Render, Railway, Fly, Koyeb, Cloud Run) are
# honoured: the service binds $AGENT_LINUX_PORT, then $PORT, then 3100. See
# deploy/ for per-platform configs and deploy/README.md for the trade-offs.
# ============================================================================ #
FROM python:3.12-slim AS runtime

# bash     — the shell every session spawns
# ca-certificates — makes curl/pip/model endpoints work inside a session
# procps   — `ps`/`top`, which a terminal without them feels broken without
# git, curl — the two tools people reach for first in a fresh shell
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git procps tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change doesn't reinstall the world.
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# The package keeps its name so `python3 -m agent_linux.service` works exactly as
# it does in the full checkout.
COPY . /app/agent_linux

# Where shells start (AGENT_LINUX_BUILD_ROOT). Never the app's own source tree.
RUN mkdir -p /app/build
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENT_LINUX_BUILD_ROOT=/app/build \
    AGENT_LINUX_PORT=3100 \
    AGENT_LINUX_HOST=0.0.0.0

EXPOSE 3100

# tini reaps the PTY children: without it, closing a tab can leave a zombie and
# a long-lived host slowly fills its process table.
ENTRYPOINT ["/usr/bin/tini", "--"]

# The liveness probe an orchestrator (or a free-tier platform) can point at.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os,urllib.request,sys; \
p=os.environ.get('AGENT_LINUX_PORT') or os.environ.get('PORT') or '3100'; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4).status==200 else 1)"

CMD ["python3", "-m", "agent_linux.service"]
