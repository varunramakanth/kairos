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

# Run live tests against real services (costs money; needs credentials)
test-live *args:
    LIVE=1 uv run pytest -m live {{ args }}

# Run eval suites (offline, cassette-backed)
evals *args:
    # pytest exits 5 when nothing is collected; allowed until the first suite lands in M1.
    uv run pytest -m evals {{ args }} || [ $? -eq 5 ]

# Regenerate the TypeScript API client from FastAPI's OpenAPI schema (arrives in M2)
gen-client:
    @echo "gen-client: no API yet; the OpenAPI -> TypeScript client lands in M2." && exit 1

# Everything CI runs
check: lint typecheck test evals
