# Kairos API with the embedded worker (lean deployment, ADR-017).
# Build from the repository root:  docker build -f infra/docker/api.Dockerfile .
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
# The workspace lists the CDK app as a member, so its metadata must exist; only the
# API package and what it depends on are installed.
COPY infra/cdk/pyproject.toml infra/cdk/pyproject.toml
COPY infra/cdk/src infra/cdk/src
RUN uv sync --frozen --no-dev --package kairos-api --no-editable

# Optional: Chromium for the browser tools (adds ~400 MB). Off in the lean build.
ARG WITH_BROWSER=false
RUN if [ "$WITH_BROWSER" = "true" ]; then playwright install --with-deps chromium; fi

RUN useradd --create-home --uid 10001 kairos && install -d -o kairos /opt/models
USER kairos
# Bake the local embedding model (~64 MB) so tasks never download it at start.
ENV XDG_CACHE_HOME=/opt/models
RUN python -c "import asyncio; from kairos_memory.embeddings import LocalEmbedder; \
asyncio.run(LocalEmbedder().embed_query('warm up'))"

EXPOSE 8000
# Migrations are idempotent (Alembic), so every start applies any new ones first.
CMD ["sh", "-c", "kairos migrate && exec uvicorn --factory kairos_api.app:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
