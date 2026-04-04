# Claude Code Instructions — LeStash

## Project

Personal knowledge base CLI + web UI. UV workspace monorepo (Python 3.12+). Aggregates content from LinkedIn, Bluesky, YouTube, arXiv, Micro.blog into SQLite with FTS5 + vector search. Parent-child item grouping (reactions/comments under posts). FastAPI HTTPS server + Tauri v2 desktop/mobile app.

## Structure

```
packages/lestash/          # Core: CLI (Typer), database, config, plugin loader
packages/lestash-server/   # FastAPI server (port 8444, Tailscale TLS)
packages/lestash-{source}/ # Source plugins (linkedin, bluesky, youtube, arxiv, microblog)
app/                       # Tauri v2 app (single-file HTML frontend)
deploy/                    # Systemd services + sync timer
```

## Commands

```bash
uv sync --dev              # Install deps
uv run just check          # Lint + format + typecheck + tests
uv run just test-all       # All package tests
uv run just server         # Start API server
just deploy                # Deploy systemd services
cd app && npx tauri dev    # Run desktop app
```

## Key Patterns

- Plugins register via `[project.entry-points."lestash.sources"]`
- `sync()` yields `ItemCreate` objects; CLI/server does UPSERT (including `parent_id`)
- Post-sync hooks resolve parent references (e.g., LinkedIn reactions → parent post)
- Display helpers in `core/enrichment.py` (shared by CLI + server)
- SQLite WAL mode for concurrent access
- `datetime(created_at)` in ORDER BY for timezone-safe sorting
- Default item listings filter `parent_id IS NULL`; children shown in detail view
- Single `index.html` frontend — no framework, dual Tauri/browser mode

## Documentation

- [`docs/api.md`](docs/api.md) — REST API reference (all 45 endpoints)
- [`extensions/chrome/README.md`](extensions/chrome/README.md) — Chrome extension usage and architecture

## Android Build

The Android project lives in `app/src-tauri/gen/android/` and is committed to the repo. This replaces the previous approach of generating it at CI time with `tauri android init` and patching with `sed`.

### Modifying the Android manifest

Edit `app/src-tauri/gen/android/app/src/main/AndroidManifest.xml` directly. Intent filters, permissions, and other manifest entries are version-controlled and reviewable in PRs.

### Custom MainActivity

The share intent handler is at `app/src-tauri/gen/android/app/src/main/java/dev/lestash/app/MainActivity.kt`. A copy is also kept at `app/android-src/MainActivity.kt` for reference.

### Upgrading Tauri

When upgrading the Tauri CLI version:
1. Create a throwaway branch
2. Run `cd app && npx tauri android init` (requires Android SDK)
3. Diff the regenerated `gen/android/` against the committed version
4. Merge changes manually, preserving custom manifest entries and MainActivity
5. Test the build with `npx tauri android build`

## When Making Changes

1. Run `uv run ruff check packages/` and `uv run ruff format --check packages/`
2. Run `uv run mypy packages/`
3. Run `uv run just test-all`
4. Follow Angular commit convention: `feat(scope):`, `fix(scope):`, `chore:`
