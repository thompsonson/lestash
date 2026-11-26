# Claude Code Instructions — LeStash

## Project

Personal knowledge base CLI + web UI. UV workspace monorepo (Python 3.12+). Aggregates content from LinkedIn, Bluesky, YouTube, arXiv, Micro.blog into SQLite with FTS5 + vector search. Parent-child item grouping (reactions/comments under posts). FastAPI HTTPS server + Tauri v2 desktop/mobile app.

## Structure

```
packages/lestash/          # Core: CLI (Typer), database, config, plugin loader
packages/lestash-server/   # FastAPI server (port 8444, Tailscale TLS)
packages/lestash-{source}/ # Source plugins (linkedin, bluesky, youtube, arxiv, microblog)
app/                       # Tauri v2 app; web frontend in app/src (ES modules, see below)
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

## LeStash CLI

Run as `uv run lestash <group> <command>` from the main checkout; `--help` on any group lists the rest.

- **Command groups:**
  - Data: `items`, `sources`, `pdf`, `media`, `profiles`.
  - Background work: `embeddings`, `enrich`.
  - Admin: `db`, `config`.
  - Accounts and sources: `google`, and one group per source, such as `youtube`, `linkedin`, `gmail` or `kobo`.
- **Syncing:** `sources sync <name>` syncs one source; `sources sync --all` is what the timer runs; `sources status` shows the last runs.
- **Read-only checks:** only these never write:
  - `db status` shows a health dashboard.
  - `db integrity` runs integrity and foreign key checks and finds orphans.
  - `db analyze` reports the largest items, items per source, duplicates, FTS health and embedding coverage.
  - `db backup list` lists backups; `db backup verify` checks one.
  - `--help` on any command, and `--dry-run` where a command has it.
- **Every other command can change live data.** On pop-mini, ask first and take `db backup create --label pre-<what>` before it, except for syncs and the timers' own jobs. Ones to know:
  - `db repair` deletes rows whose item is gone; without `--confirm` it shows the counts and asks first (#400).
  - `db history --purge` removes old history rows; it needs `--before YYYY-MM-DD` or `--keep-days N`.
  - `db optimize` does nothing without a flag: `--vacuum`, `--fts`, `--checkpoint`, `--stats`, or `--all`.
  - `db link`, `db relate`, `db themes` and `db agents` re-derive links, suggestions and agents.
- **Backups:** `db backup create | list | verify | extract | compress | prune`; the rules are under Database backups below.

## Key Patterns

- Plugins register via `[project.entry-points."lestash.sources"]`
- `sync()` yields `ItemCreate` objects; CLI/server does UPSERT (including `parent_id`)
- Post-sync hooks resolve parent references (e.g., LinkedIn reactions → parent post)
- Display helpers in `core/enrichment.py` (shared by CLI + server)
- SQLite WAL mode for concurrent access
- No slow work (network, files, long scans) while a write is open: prepare first, then write in `write_transaction(conn)` from `core/database.py` (#401)
- `datetime(created_at)` in ORDER BY for timezone-safe sorting
- Default item listings filter `parent_id IS NULL`; children shown in detail view
- Frontend: plain ES modules, no framework, no build step; one codebase for Tauri and the browser
  - `app/src/index.html` is the shell; `js/router.js` maps each address to exactly one page
  - Organised by object, as in `docs/ux-object-model-design.md`: `js/objects/<object>/` holds its `card.js` and `page.js`; `js/places/` holds Timeline, Library, Search, People, Settings
  - Code still in the old single file moves out one waypoint at a time (#268); new screens go in modules, never back into it
  - Screens use the object names from the design doc (Item, Person, Channel, Activity, File, Topic, Collection)

## Database backups

- Before a deploy, migration or data cleanup: `lestash db backup create --label pre-<what>`, e.g. `pre-deploy-355`.
- Never back up with `sqlite3 .backup`, `cp` or a hand-picked name; the command checks, names and prunes copies.
- `create` exits 1 if the copy fails its integrity check; stop the change if it does.
- `lestash-backup.timer` runs `create --keep 3` daily at 03:50, before sysbak's 04:00 snapshot; it prunes only unlabelled copies.
- Labelled copies stay until `lestash db backup prune --labelled --keep N`; ask before running it on pop-mini.
- sysbak copies `~/.local/share/lestash/backups/`, never the live `lestash.db`, `-wal` or `-shm`.
- Details, the sysbak excludes and the restore steps: [`docs/backups.md`](docs/backups.md).

## Documentation

- [`docs/api.md`](docs/api.md) — REST API reference (all endpoints)
- [`docs/backups.md`](docs/backups.md) — database backups: commands, schedule, sysbak excludes, restore
- [`docs/markdown-notes-schema.md`](docs/markdown-notes-schema.md) — frontmatter a markdown notes repository uses for LeStash to import it
- [`docs/content-and-activity-ontology-design.md`](docs/content-and-activity-ontology-design.md) — the ontology: items, links, people, activities
- [`docs/ux-object-model-design.md`](docs/ux-object-model-design.md) — the app's objects, navigation and wireframes; roadmap #268
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
