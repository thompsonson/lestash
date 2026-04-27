# Le Stash

A personal knowledge base CLI that aggregates content from multiple sources into a unified, searchable database.

This is born from not having access to LinkedIn posts that are over a year old (info [more context](https://www.linkedin.com/posts/thompson-m_some-linkedin-skills-i-wanted-to-reference-activity-7418659433720930304-XUC7?utm_source=share&utm_medium=member_desktop&rcm=ACoAAABtQ-4BolpwOKeRo9vZaIfsjv32nWMFlBc))

## Features

- **Multi-source aggregation** - Import content from LinkedIn, Bluesky, Micro.blog, arXiv, and more
- **Plugin architecture** - Extensible design for adding new data sources
- **Full-text search** - SQLite FTS5 for fast content search
- **Vector search** - Semantic similarity search using sqlite-vec and sentence-transformers
- **Parent-child grouping** - Reactions, comments, and replies grouped under their parent post
- **Person profiles** - Map URNs to names and profile URLs for better display
- **Draft export** - Create Micro.blog drafts from saved items for the search→write workflow
- **Audit history** - Track changes to items over time
- **CLI-first** - Built with Typer for a modern command-line experience
- **API server** - HTTPS REST API for accessing your knowledge base from any device
- **Desktop app** - Tauri v2 cross-platform app (macOS, Linux) with browser fallback

## Install

### Desktop App

Download the latest build from [Releases](../../releases):

| Platform | Format | Install |
|----------|--------|---------|
| **macOS** | `.dmg` | Open, drag to Applications |
| **Linux** | `.deb` | `sudo dpkg -i lestash_*.deb` |
| **Linux** | `.AppImage` | `chmod +x lestash_*.AppImage && ./lestash_*.AppImage` |
| **Android** | `.apk` | Install directly or use [Obtainium](https://obtainium.imranr.dev/) for auto-updates |

Or use the install script (requires [gh CLI](https://cli.github.com/)):

```bash
bash <(curl -s https://raw.githubusercontent.com/thompsonson/lestash/main/scripts/install-desktop.sh)
```

### Browser

No install needed — browse to your server URL (e.g., `https://pop-mini:8444/`).

### CLI (from source)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:thompsonson/lestash.git
cd lestash
uv sync --dev
uv run lestash --help
```

## Quick Start

```bash
# Initialize configuration
uv run lestash config init

# Search arXiv and save a paper
uv run lestash arxiv search "transformer attention"
uv run lestash arxiv save 1706.03762

# List saved items (with date/time and author info)
uv run lestash items list

# Search your knowledge base
uv run lestash items search "attention mechanism"

# Show item details (includes URLs for reactions/comments)
uv run lestash items show 42

# Create a Micro.blog draft from an item
uv run lestash items draft 42 --output ~/blog/content/drafts/
```

### Person Profiles

Map LinkedIn URNs to human-readable names:

```bash
# Add a profile mapping
uv run lestash profiles add "urn:li:person:abc123" --name "John Doe" --url "https://linkedin.com/in/johndoe"

# List configured profiles
uv run lestash profiles list
```

## Project Structure

This is a UV workspace monorepo:

```
le-stash/
├── packages/
│   ├── lestash/           # Core CLI and plugin system
│   ├── lestash-arxiv/     # arXiv source plugin
│   ├── lestash-bluesky/   # Bluesky source plugin
│   ├── lestash-linkedin/  # LinkedIn source plugin
│   ├── lestash-microblog/ # Micro.blog source plugin
│   ├── lestash-youtube/   # YouTube source plugin
│   └── lestash-server/    # HTTPS API server
├── app/                   # Tauri v2 desktop app
├── deploy/                # Systemd service files
├── pyproject.toml         # Workspace configuration
└── uv.lock                # Locked dependencies
```

| Package | Description |
|---------|-------------|
| [lestash](packages/lestash/) | Core CLI, database, configuration, and plugin loader |
| [lestash-arxiv](packages/lestash-arxiv/) | Search and save arXiv papers |
| [lestash-bluesky](packages/lestash-bluesky/) | Sync and search Bluesky posts |
| [lestash-linkedin](packages/lestash-linkedin/) | Import LinkedIn posts via DMA Portability API |
| [lestash-microblog](packages/lestash-microblog/) | Sync posts from Micro.blog |
| [lestash-youtube](packages/lestash-youtube/) | Sync liked videos and subscriptions from YouTube |
| [lestash-server](packages/lestash-server/) | HTTPS REST API server (FastAPI) |

## Development

This project uses [just](https://github.com/casey/just) as a command runner. After installing dependencies, you can use `just` to run common tasks:

```bash
# Install dependencies (including just)
uv sync --dev

# List all available commands
uv run just

# Run all tests
uv run just test-all

# Run tests for a specific package
uv run just test lestash-bluesky

# Run linting and fix issues
uv run just lint-fix

# Format code
uv run just format

# Run all quality checks (lint, format, typecheck, tests)
uv run just check

# Show project statistics
uv run just stats
```

### Manual commands (without just)

```bash
# Run linting
uv run ruff check packages/

# Run tests
uv run pytest packages/lestash/tests
uv run pytest packages/lestash-bluesky/tests
uv run pytest packages/lestash-linkedin/tests

# Run pre-commit hooks
uv run pre-commit run --all-files
```

## API Server

The API server provides HTTPS access to your knowledge base from any device on your network.

```bash
# Start the server (uses Tailscale TLS certs if available, HTTP fallback otherwise)
uv run lestash-server

# Or with explicit options
uv run lestash-server --port 8444 --cert /path/to/cert.crt --key /path/to/key.key
```

Endpoints: `/api/health`, `/api/items`, `/api/items/search`, `/api/items/{id}`, `/api/items/{id}/children`, `/api/items/{id}/similar`, `/api/sources`, `/api/profiles`, `/api/stats`

API docs available at `/api/docs` when the server is running.

### Deployment

```bash
# Deploy as a systemd user service
just deploy

# Management
just server-status
just server-logs
just server-restart
```

## Desktop App

A Tauri v2 desktop app wraps the web UI for native access.

```bash
# Development
cd app && npm install && npx tauri dev

# Build
cd app && npx tauri build
```

## Configuration

Configuration is stored in `~/.config/lestash/config.toml`:

```toml
[general]
database_path = "~/.config/lestash/lestash.db"

[logging]
level = "INFO"
```

## License

[GNU Affero General Public License v3.0 only](LICENSE) (AGPL-3.0-only).

Copyright (C) 2025–2026 Matthew Thompson.

LeStash uses [PyMuPDF](https://pymupdf.readthedocs.io/) (AGPL) for PDF enrichment, which obliges the project to use a compatible license. Practical implication of AGPL §13: if you run a *modified* copy of LeStash as a network service that other people interact with, you must offer those users access to the source. Personal single-user deployments are unaffected.
