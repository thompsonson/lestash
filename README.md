# Le Stash

A personal knowledge base CLI that aggregates content from multiple sources into a unified, searchable database.

This is born from not having access to LinkedIn posts that are over a year old (info [more context](https://www.linkedin.com/posts/thompson-m_some-linkedin-skills-i-wanted-to-reference-activity-7418659433720930304-XUC7?utm_source=share&utm_medium=member_desktop&rcm=ACoAAABtQ-4BolpwOKeRo9vZaIfsjv32nWMFlBc))

## Features

- **Multi-source aggregation** - Import content from LinkedIn, Bluesky, Micro.blog, arXiv, and more
- **Plugin architecture** - Extensible design for adding new data sources
- **Full-text search** - SQLite FTS5 for fast content search
- **Person profiles** - Map URNs to names and profile URLs for better display
- **Draft export** - Create Micro.blog drafts from saved items for the search→write workflow
- **Audit history** - Track changes to items over time
- **CLI-first** - Built with Typer for a modern command-line experience

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# Clone the repository
git clone git@github.com:thompsonson/lestash.git
cd lestash

# Install dependencies (including dev tools like just)
uv sync --dev

# Verify installation
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
│   └── lestash-microblog/ # Micro.blog source plugin
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

## Configuration

Configuration is stored in `~/.config/lestash/config.toml`:

```toml
[general]
database_path = "~/.config/lestash/lestash.db"

[logging]
level = "INFO"
```

## License

[MIT](LICENSE)
