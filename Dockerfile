FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project   # deps layer, cached across code changes
COPY . .
RUN uv sync --frozen --no-dev
EXPOSE 8080
CMD ["uv", "run", "uvicorn", "incant.server:app", "--host", "0.0.0.0", "--port", "8080"]
