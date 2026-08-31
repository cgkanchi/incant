# Base MUST match .python-version: with a mismatched base, uv downloads its own
# interpreter into /root/.local (0700) and the non-root runtime user can't reach
# it. UV_PYTHON_DOWNLOADS=never turns that failure mode into a loud build error.
FROM python:3.12-slim
ENV UV_PYTHON_DOWNLOADS=never
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client curl \
    && rm -rf /var/lib/apt/lists/*
# Pinned by major.minor (not :latest): builds stay reproducible without freezing
# out patch releases. Bump deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

# Run as a non-root user that owns the one durable mount (the canonical repo).
RUN useradd --create-home --uid 10001 incant \
    && mkdir -p /var/lib/incant/repo \
    && chown -R incant:incant /var/lib/incant

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Workspace members must be present for uv to load the workspace, even though
# --no-dev leaves them uninstalled (they are dev-group deps for the test suites).
COPY sdk/python/pyproject.toml sdk/python/
COPY mcp/python/pyproject.toml mcp/python/
RUN uv sync --frozen --no-dev --no-install-project   # deps layer, cached across code changes
COPY . .
RUN uv sync --frozen --no-dev && chown -R incant:incant /app
# The environment is complete: `uv run` must never re-sync at runtime (it would pull
# the dev group — test deps + workspace members — over the network on every start).
ENV UV_NO_SYNC=1

USER incant
ENV INCANT_REPO_PATH=/var/lib/incant/repo
EXPOSE 8080
# Exactly ONE worker per container: the snapshot/auth caches, publish lock, and
# failed-auth throttle are per-process. Scale with more containers (INCANT_MODE=serve
# replicas), never with --workers.
CMD ["uv", "run", "uvicorn", "incant.server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
