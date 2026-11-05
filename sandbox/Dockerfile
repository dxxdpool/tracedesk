# Sandbox for approved experiments: Python 3.12 stdlib + pytest, nothing else.
# Build: docker build -t tracedesk-sandbox sandbox/
# `st experiment run` starts it with no network, a read-only filesystem and the repo mounted read-only.
FROM python:3.12-slim

# Pinned so an experiment means the same thing on every machine.
RUN pip install --no-cache-dir pytest==8.3.5 \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin runner

USER 1000:1000
WORKDIR /work
