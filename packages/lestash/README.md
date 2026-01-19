# lestash

Core CLI and plugin system for Le Stash - a personal knowledge base.

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
# List all items
lestash items list

# Search items
lestash items search "query"

# Show item details
lestash items show <id>

# Export items to JSON
lestash items export --output backup.json
```

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
