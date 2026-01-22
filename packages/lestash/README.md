# lestash

[![PyPI version](https://badge.fury.io/py/lestash.svg)](https://pypi.org/project/lestash/)
[![GitHub](https://img.shields.io/github/license/thompsonson/lestash)](https://github.com/thompsonson/lestash)

Core CLI and plugin system for Le Stash - a personal knowledge base that aggregates content from multiple sources into a unified, searchable database.

> **Note**: For project overview, features, and quick start guide, see the [GitHub repository](https://github.com/thompsonson/lestash).

## Installation

```bash
uv add lestash
```

Or install the full workspace from the repository root:

```bash
uv sync --all-packages
```

## CLI Commands

### Items

```bash
# List all items (with date/time and resolved author names)
lestash items list

# Filter by source
lestash items list --source linkedin

# Search items
lestash items search "query"

# Show item details (includes reaction/comment target URLs)
lestash items show <id>

# Export items to JSON
lestash items export --output backup.json

# Create a Micro.blog draft from an item
lestash items draft <id> --output ~/blog/content/drafts/
```

### Profiles

Map person URNs (e.g., LinkedIn) to human-readable names and profile URLs:

```bash
# Add a profile mapping
lestash profiles add "urn:li:person:abc123" --name "John Doe" --url "https://linkedin.com/in/johndoe"

# List all profiles
lestash profiles list

# Show a specific profile
lestash profiles show "urn:li:person:abc123"

# Remove a profile
lestash profiles remove "urn:li:person:abc123"
```

When profiles are configured, `items list` and `items search` display names instead of URNs, and `items show` includes the profile URL.

### Sources

```bash
# List configured sources
lestash sources list

# Sync all sources
lestash sources sync

# View sync history
lestash sources history
```

### Configuration

```bash
# Show current config
lestash config show

# Initialize config file
lestash config init

# Set a config value
lestash config set logging.level DEBUG
```

## Plugin Architecture

Le Stash uses entry points for plugin discovery. Plugins implement the `SourcePlugin` base class:

```python
from lestash.plugins.base import SourcePlugin
from lestash.models.item import ItemCreate

class MySource(SourcePlugin):
    name = "my-source"
    description = "My custom data source"

    def get_commands(self) -> typer.Typer:
        """Return Typer app with source-specific commands."""
        app = typer.Typer()
        # Add commands...
        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Fetch items from the source."""
        yield ItemCreate(
            source_type="my-source",
            source_id="unique-id",
            content="Item content",
        )
```

Register the plugin in `pyproject.toml`:

```toml
[project.entry-points."lestash.sources"]
my-source = "my_package:MySource"
```

## Data Model

### Item

The core unit of content:

| Field | Type | Description |
|-------|------|-------------|
| `id` | int | Auto-generated primary key |
| `source_type` | str | Plugin identifier (e.g., "arxiv", "linkedin") |
| `source_id` | str | Unique ID within the source |
| `url` | str | Optional URL to original content |
| `title` | str | Optional title |
| `content` | str | Main text content |
| `author` | str | Optional author |
| `created_at` | datetime | When the content was created |
| `is_own_content` | bool | Whether user authored this |
| `metadata` | dict | Source-specific data (JSON) |

### Database

SQLite database with:

- **items** - Main content table with unique constraint on (source_type, source_id)
- **items_fts** - Full-text search virtual table (FTS5)
- **item_history** - Audit trail of content changes
- **person_profiles** - URN to name/URL mapping for display
- **tags** / **item_tags** - Tagging system
- **sources** - Plugin configuration storage
- **sync_log** - Sync operation history

## Configuration

Default location: `~/.config/lestash/config.toml`

```toml
[general]
database_path = "~/.config/lestash/lestash.db"

[logging]
level = "INFO"
file_path = "~/.config/lestash/logs/lestash.log"
```

## License

[MIT](../../LICENSE)
