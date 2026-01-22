"""Main CLI for Le Stash."""

import typer

from lestash import __version__
from lestash.cli import config, items, profiles, sources
from lestash.core.database import init_database
from lestash.core.logging import get_console, get_logger, setup_logging
from lestash.plugins.loader import load_plugins

app = typer.Typer(
    name="lestash",
    help="Le Stash - Personal knowledge base CLI.",
    no_args_is_help=True,
)
console = get_console()
logger = get_logger("cli.main")

# Register core command groups
app.add_typer(items.app, name="items")
app.add_typer(sources.app, name="sources")
app.add_typer(config.app, name="config")
app.add_typer(profiles.app, name="profiles")


def register_plugin_commands() -> None:
    """Discover and register plugin commands."""
    plugins = load_plugins()
    for name, plugin in plugins.items():
        try:
            plugin_app = plugin.get_commands()
            app.add_typer(plugin_app, name=name)
        except Exception as e:
            logger.warning(f"Failed to register commands for '{name}': {e}")


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", "-v", help="Show version and exit."),
) -> None:
    """Le Stash - Personal knowledge base CLI."""
    # Initialize logging first
    setup_logging()

    if version:
        console.print(f"lestash {__version__}")
        raise typer.Exit()

    # Ensure database exists
    init_database()


# Register plugin commands at import time
register_plugin_commands()

if __name__ == "__main__":
    app()
