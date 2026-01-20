# Le Stash

A personal knowledge base CLI that aggregates content from multiple sources into a unified, searchable database.

This is born from not having access to LinkedIn posts that are over a year old (info [more context](https://www.linkedin.com/posts/thompson-m_some-linkedin-skills-i-wanted-to-reference-activity-7418659433720930304-XUC7?utm_source=share&utm_medium=member_desktop&rcm=ACoAAABtQ-4BolpwOKeRo9vZaIfsjv32nWMFlBc))

## Features

- **Multi-source aggregation** - Import content from LinkedIn, arXiv, and more
- **Plugin architecture** - Extensible design for adding new data sources
- **Full-text search** - SQLite FTS5 for fast content search
- **Audit history** - Track changes to items over time
- **CLI-first** - Built with Typer for a modern command-line experience

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# Clone the repository
git clone git@github.com:thompsonson/lestash.git
cd lestash

# Install dependencies
uv sync --all-packages

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

# List saved items
uv run lestash items list

# Search your knowledge base
uv run lestash items search "attention mechanism"
```

## Project Structure

This is a UV workspace monorepo:

```
le-stash/
├── packages/
│   ├── lestash/           # Core CLI and plugin system
│   ├── lestash-arxiv/     # arXiv source plugin
│   └── lestash-linkedin/  # LinkedIn source plugin
├── pyproject.toml         # Workspace configuration
└── uv.lock                # Locked dependencies
```

| Package | Description |
|---------|-------------|
| [lestash](packages/lestash/) | Core CLI, database, configuration, and plugin loader |
| [lestash-arxiv](packages/lestash-arxiv/) | Search and save arXiv papers |
| [lestash-linkedin](packages/lestash-linkedin/) | Import LinkedIn posts via DMA Portability API |

## Development

```bash
# Install with dev dependencies
uv sync --all-packages

# Run linting
uv run ruff check packages/*/src

# Run tests
uv run pytest packages/lestash/tests
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
