"""Config commands for Le Stash CLI."""

import tomllib
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

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
        # escape(): Rich would read "[general]" as a style tag and drop it
        console.print(f"[bold]{escape(f'[{section}]')}[/bold]")
        if isinstance(values, dict):
            for key, value in values.items():
                console.print(f"  {key} = {escape(str(value))}")
        else:
            console.print(f"  {escape(str(values))}")
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


def parse_value(raw: str) -> Any:
    """A TOML value when ``raw`` is one (``true``, ``5``, ``["a"]``, ``"quoted"``), else ``raw``.

    So ``lestash config set kobo.include_store_titles false`` stores a boolean, not "false".
    Dates and tables stay text: no setting takes one.
    """
    try:
        value = tomllib.loads(f"value = {raw}")["value"]
    except tomllib.TOMLDecodeError:
        return raw
    return value if isinstance(value, bool | int | float | str | list) else raw


def _known_sections() -> dict[str, set[str] | None]:
    """Section -> its keys; None for a source plugin's section, whose keys the plugin owns."""
    from lestash.plugins.loader import load_plugins

    sections: dict[str, set[str] | None] = {
        name: set(type(model).model_fields) for name, model in Config()
    }
    for name in load_plugins():
        sections.setdefault(name, None)
    return sections


@app.command("set")
def set_config(
    key: Annotated[str, typer.Argument(help="Config key (e.g., general.database_path)")],
    value: Annotated[str, typer.Argument(help='Value: true, 5, ["a", "b"] or text')],
) -> None:
    """Set a value in [general], [logging] or a source plugin's section."""
    config = Config.load()

    parts = key.split(".")
    if len(parts) != 2:
        console.print("[red]Key must be in format 'section.key'[/red]")
        raise typer.Exit(1)

    section, setting = parts

    # Refuse typos, which would otherwise be saved where nothing reads them
    known = _known_sections()
    if section not in known:
        console.print(f"[red]Unknown section: {section}[/red]")
        console.print(f"[dim]Known: {', '.join(sorted(known))}[/dim]")
        raise typer.Exit(1)
    allowed = known[section]
    if allowed is not None and setting not in allowed:
        console.print(f"[red]Unknown key in {escape(f'[{section}]')}: {setting}[/red]")
        console.print(f"[dim]Known: {', '.join(sorted(allowed))}[/dim]")
        raise typer.Exit(1)

    data = config.model_dump()
    current = data.get(section)
    if not isinstance(current, dict):
        current = data[section] = {}
    parsed = parse_value(value)
    current[setting] = parsed

    # Save updated config
    try:
        updated_config = Config(**data)
        updated_config.save()
        console.print(f"[green]Set {key} = {parsed!r}[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None
