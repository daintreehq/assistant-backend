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
# Prompt/runbook files ship as package data. requirements.lock is a constraints
# file: it pins versions but only the runtime deps from pyproject get installed.
COPY pyproject.toml README.md requirements.lock ./
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY third_party ./third_party
COPY src ./src
RUN pip install . -c requirements.lock


# ---- Runtime: minimal, non-root -------------------------------------------
FROM python:3.12-slim AS runtime

# Stamped at build time so /version reports a real commit instead of an empty
# string: `docker build --build-arg BUILD_SHA=$(git rev-parse HEAD) .`
ARG BUILD_SHA=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8080 \
    BUILD_SHA=${BUILD_SHA}

RUN useradd --create-home --uid 10001 daintree

COPY --from=builder /opt/venv /opt/venv

USER daintree
WORKDIR /home/daintree

EXPOSE 8080

# Cloud Run injects $PORT (and HOST=0.0.0.0 is set above). One ASGI process; the
# entrypoint binds HOST/PORT and handles graceful shutdown within Cloud Run's
# termination window.
CMD ["python", "-m", "daintree_assistant_server"]


# ---- Qualify: the release gate, on the builder's venv ----------------------
# Built only by cloudbuild.yaml's `qualify` step (`--target=qualify`), never into
# the image that ships. FROM the builder because its /opt/venv is the layer the
# runtime copies: the checks below import the INSTALLED package (the same
# catalog and selector files, the same onnxruntime on the same Linux base), and
# `identity` reads what that venv serves before the test tools are added beside
# it, so the record's runtime is the runtime image's own.
FROM builder AS qualify

ARG BUILD_SHA=""

WORKDIR /qualify
COPY pyproject.toml requirements.lock selector.lock ./
COPY src ./src
COPY tests ./tests
COPY benchmarks ./benchmarks
RUN python benchmarks/qualify_release.py identity > identity.json
RUN pip install -c requirements.lock pytest pytest-asyncio pytest-mock httpx jsonschema
# The commit goes in as an argument, not as the environment: an ARG is exported
# to RUN, and the tests' Settings would read it as the server's own BUILD_SHA.
RUN env -u BUILD_SHA python benchmarks/qualify_release.py run --root /qualify \
      --build-sha "$BUILD_SHA" --identity identity.json --out qualification.json
