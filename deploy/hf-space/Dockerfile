# syntax=docker/dockerfile:1
# ============================================================================ #
# NovaRouter Terminal — Hugging Face Spaces build.
#
# Copy this package to the Space root and use this file as `Dockerfile`. Two
# deviations from the generic image, both because of how Spaces works:
#
#   1. port 7860 — Spaces routes the public URL at `app_port` in README.md.
#   2. /data       — the only path a Space keeps across restarts, so the shell
#                    workspace lives there instead of /app/build.
#
# A Space is a *free host that does not sleep after 15 minutes*: cpu-basic is
# paused after 48h idle, not 15m. That makes it the best free tier for a
# terminal, at the cost of a public-by-default URL — hence the token.
# ============================================================================ #
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git procps tini \
    && rm -rf /var/lib/apt/lists/*

# Spaces run as a non-root user by default; the shell still needs somewhere
# writable, and /data is the mount that survives.
RUN mkdir -p /app /data/build \
    && chmod -R 777 /data

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY . /app/terminal

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOVA_BUILD_ROOT=/data/build \
    NOVA_TERMINAL_PORT=7860 \
    NOVA_TERMINAL_HOST=0.0.0.0 \
    HOME=/data

EXPOSE 7860

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "terminal.service"]