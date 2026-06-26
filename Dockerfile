# syntax=docker/dockerfile:1

# ---- Builder: install the package + deps into a venv -----------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy sources and install against the pinned lock for reproducible builds.
# Prompt/skill files ship as package data. requirements.lock is a constraints
# file: it pins versions but only the runtime deps from pyproject get installed.
COPY pyproject.toml README.md requirements.lock ./
COPY src ./src
RUN pip install . -c requirements.lock

# ---- Runtime: minimal, non-root -------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080

RUN useradd --create-home --uid 10001 daintree

COPY --from=builder /opt/venv /opt/venv

USER daintree
WORKDIR /home/daintree

EXPOSE 8080

# Cloud Run injects $PORT. One ASGI process; Uvicorn handles graceful shutdown
# on SIGTERM within Cloud Run's termination window.
CMD ["sh", "-c", "exec uvicorn daintree_assistant_server.main:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-graceful-shutdown 25"]
