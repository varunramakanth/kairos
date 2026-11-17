# Kairos: single entrypoint for every dev task. Run `just` to list recipes.

set dotenv-load := true

compose := "docker compose -f infra/compose.yaml"

# List recipes
default:
    @just --list

# Install Python and web dependencies
sync:
    uv sync --all-packages
    # macOS can flag files in iCloud-synced folders as hidden, and Python 3.13 skips
    # hidden .pth files, which breaks editable installs (see CLAUDE.md).
    @if [ "$(uname)" = "Darwin" ]; then chflags -R nohidden .venv; fi
    pnpm --dir apps/web install

# Start local services (Postgres + pgvector) and wait until healthy
dev:
    {{ compose }} up -d --wait postgres

# Start local services plus the Langfuse stack (http://localhost:3100)
dev-obs:
    {{ compose }} --profile obs up -d --wait

# Stop local services (data volumes are kept)
down:
    {{ compose }} --profile obs down

# Run the web dev server (http://localhost:3000)
web:
    pnpm --dir apps/web dev

# Run the API on http://localhost:8000 with an embedded worker (needs `just dev`)
api:
    KAIROS_EMBEDDED_WORKER=1 uv run uvicorn --factory kairos_api.app:create_app --port 8000 --reload

# Same, but the model is a scripted demo cassette: no API key, no cost
api-demo:
    KAIROS_EMBEDDED_WORKER=1 KAIROS_PROVIDER=replay KAIROS_MODEL=claude-opus-5-5 \
      KAIROS_REPLAY_CASSETTE=tests/e2e/cassettes/demo.jsonl TAVILY_API_KEY= \
      uv run uvicorn --factory kairos_api.app:create_app --port 8000

# Format Python and web code
fmt:
    uv run ruff format .
    uv run ruff check --fix .
    pnpm --dir apps/web lint:fix

# Lint Python (Ruff) and web (Biome); no files are changed
lint:
    uv run ruff format --check .
    uv run ruff check .
    pnpm --dir apps/web lint

# Typecheck Python (pyright strict) and web (tsc)
typecheck:
    uv run pyright
    pnpm --dir apps/web typecheck

# Run offline tests (no network; cassettes only). Extra args go to pytest.
test *args:
    uv run pytest -m "not evals" {{ args }}
    pnpm --dir apps/web test

# Run live tests against real services (costs money; needs credentials)
test-live *args:
    LIVE=1 uv run pytest -m live {{ args }}

# Run eval suites (offline, cassette-backed)
evals *args:
    uv run pytest -m evals {{ args }}

# Rebuild eval cassettes (today: from the synthetic scripts in tests/evals/scenarios.py)
record-cassettes:
    uv run python -m tests.evals.record_cassettes

# Apply database migrations to the local Postgres
migrate:
    uv run kairos migrate

# Run the durable worker against the local Postgres
worker:
    uv run kairos worker

# Regenerate the TypeScript API client from FastAPI's OpenAPI schema
gen-client:
    uv run python -m kairos_api.openapi apps/web/openapi.json
    pnpm --dir apps/web gen-client

# Everything CI runs
check: lint typecheck test evals
