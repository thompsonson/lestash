# Lestash monorepo command runner
# Run `just` or `just --list` to see all available commands

# Default recipe: show available commands
default:
    @just --list

# Install all dependencies including dev tools
install:
    uv sync --dev

# Run all tests across all packages
test-all:
    @echo "Running lestash core tests..."
    uv run pytest packages/lestash/tests
    @echo "\nRunning lestash-bluesky tests..."
    uv run pytest packages/lestash-bluesky/tests
    @echo "\nRunning lestash-linkedin tests..."
    uv run pytest packages/lestash-linkedin/tests
    @echo "\nRunning lestash-microblog tests..."
    uv run pytest packages/lestash-microblog/tests
    @echo "\nRunning lestash-youtube tests..."
    uv run pytest packages/lestash-youtube/tests
    @echo "\nRunning lestash-audible tests..."
    uv run pytest packages/lestash-audible/tests
    @echo "\nRunning lestash-server tests..."
    uv run pytest packages/lestash-server/tests

# Run tests for a specific package (e.g., just test lestash-bluesky)
test package:
    uv run pytest packages/{{package}}/tests -v

# Run tests with coverage report
test-coverage:
    uv run pytest packages/lestash/tests --cov=packages/lestash/src --cov-report=term-missing
    uv run pytest packages/lestash-bluesky/tests --cov=packages/lestash-bluesky/src --cov-report=term-missing
    uv run pytest packages/lestash-linkedin/tests --cov=packages/lestash-linkedin/src --cov-report=term-missing

# Run linter (ruff check) on all packages
lint:
    uv run ruff check packages/

# Fix linting issues automatically
lint-fix:
    uv run ruff check --fix packages/

# Format code with ruff
format:
    uv run ruff format packages/

# Check code formatting without changing files
format-check:
    uv run ruff format --check packages/

# Run type checker (mypy)
typecheck:
    uv run mypy packages/

# Run all quality checks (lint, format check, typecheck, tests)
check: lint format-check typecheck test-all
    @echo "\n✓ All checks passed!"

# Clean up build artifacts and caches
clean:
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true

# Show project statistics (lines of code, test count, etc.)
stats:
    @echo "=== Project Statistics ==="
    @echo "\nLines of code:"
    @find packages -name "*.py" -not -path "*/tests/*" | xargs wc -l | tail -1
    @echo "\nTest lines:"
    @find packages -name "*.py" -path "*/tests/*" | xargs wc -l | tail -1
    @echo "\nTest count:"
    @grep -r "def test_" packages/*/tests | wc -l
    @echo "\nPackages:"
    @ls -1 packages/

# Watch tests (requires entr: apt install entr or brew install entr)
watch-test package="lestash":
    find packages/{{package}} -name "*.py" | entr -c just test {{package}}

# Start the API server (dev mode)
server:
    uv run lestash-server

# Check server status (systemd)
server-status:
    systemctl --user status lestash-server

# Follow server logs (systemd)
server-logs:
    journalctl --user -u lestash-server -f

# Restart server (systemd)
server-restart:
    systemctl --user restart lestash-server

# Show sync timer status
sync-status:
    systemctl --user list-timers lestash-sync.timer --no-pager

# View sync logs
sync-logs:
    journalctl --user -u lestash-sync -n 50 --no-pager

# Trigger a sync now (without waiting for timer)
sync-now:
    systemctl --user start lestash-sync.service

# Deploy systemd service
deploy:
    bash deploy/install.sh

# Setup pre-commit hooks
setup-hooks:
    uv run pre-commit install

# Run pre-commit hooks on all files
pre-commit:
    uv run pre-commit run --all-files
