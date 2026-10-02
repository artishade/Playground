# syntax=docker/dockerfile:1
# ============================================================================ #
# NovaRouter Terminal — standalone image.
#
# Build context is THIS directory (the `terminal/` package). The image contains
# the package and three Python dependencies — no gateway, no database, no
# dashboard. Just the shells, their HTTP contract, and the Agentbox agent.
#
#   docker build -t novarouter-terminal .
#   docker run -p 3100:3100 -e NOVA_TERMINAL_TOKEN=<secret> novarouter-terminal
#
# Cloud hosts that inject $PORT (Render, Railway, Fly, Koyeb, Cloud Run) are
# honoured: the service binds $NOVA_TERMINAL_PORT, then $PORT, then 3100. See
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

# The package keeps its name so `python3 -m terminal.service` works exactly as
# it does in the full checkout.
COPY . /app/terminal

# Where shells start (NOVA_BUILD_ROOT). Never the app's own source tree.
RUN mkdir -p /app/build
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOVA_BUILD_ROOT=/app/build \
    NOVA_TERMINAL_PORT=3100 \
    NOVA_TERMINAL_HOST=0.0.0.0

EXPOSE 3100

# tini reaps the PTY children: without it, closing a tab can leave a zombie and
# a long-lived host slowly fills its process table.
ENTRYPOINT ["/usr/bin/tini", "--"]

# The liveness probe an orchestrator (or a free-tier platform) can point at.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os,urllib.request,sys; \
p=os.environ.get('NOVA_TERMINAL_PORT') or os.environ.get('PORT') or '3100'; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4).status==200 else 1)"

CMD ["python3", "-m", "terminal.service"]
