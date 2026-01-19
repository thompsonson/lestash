"""Config commands for Le Stash CLI."""

from typing import Annotated

import typer
from rich.console import Console

from lestash.core.config import Config, get_config_path, init_config

app = typer.Typer(help="Manage configuration.")
console = Console()


@app.command("show")
def show_config() -> None:
    """Show current configuration."""
    config_path = get_config_path()

    if not config_path.exists():
        console.print(f"[dim]No config file at {config_path}[/dim]")
        console.print("[dim]Run 'lestash config init' to create one.[/dim]")
        return

    config = Config.load()
    console.print(f"[bold]Config file:[/bold] {config_path}")
    console.print()

    data = config.model_dump()
    for section, values in data.items():
        console.print(f"[bold][{section}][/bold]")
        if isinstance(values, dict):
            for key, value in values.items():
                console.print(f"  {key} = {value}")
        else:
            console.print(f"  {values}")
        console.print()


@app.command("init")
def init_config_cmd(
    force: Annotated[bool, typer.Option("--force", "-f", help="Overwrite existing config")] = False,
) -> None:
    """Initialize configuration file with defaults."""
    config_path = get_config_path()

    if config_path.exists() and not force:
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("[dim]Use --force to overwrite.[/dim]")
        raise typer.Exit(1)

    init_config()
    console.print(f"[green]Created config at {config_path}[/green]")


@app.command("set")
def set_config(
    key: Annotated[str, typer.Argument(help="Config key (e.g., general.database_path)")],
    value: Annotated[str, typer.Argument(help="Value to set")],
) -> None:
    """Set a configuration value."""
    config = Config.load()

    parts = key.split(".")
    if len(parts) != 2:
        console.print("[red]Key must be in format 'section.key'[/red]")
        raise typer.Exit(1)

    section, setting = parts

    # Get current config as dict
    data = config.model_dump()

    if section not in data:
        data[section] = {}

    data[section][setting] = value

    # Save updated config
    try:
        updated_config = Config(**data)
        updated_config.save()
        console.print(f"[green]Set {key} = {value}[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None
