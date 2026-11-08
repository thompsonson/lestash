"""Config commands for Le Stash CLI."""

import re
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


# No leading zero or sign, so "007" and "+4412345" stay text
_INTEGER = re.compile(r"-?(0|[1-9]\d*)")


def parse_value(raw: str) -> Any:
    """A plugin setting's value, typed only where the text is unambiguous.

    - ``true`` / ``false``, in any case -> bool, so ``kobo.include_store_titles false``
      is false.
    - A whole number without a leading zero or ``+`` -> int, so
      ``arxiv.max_per_keyword 5`` is 5.
    - A TOML array or quoted string -> its value: ``["LLM agents", "ontology"]``, ``"5"``.
    - Anything else stays text. Decimals do too: ``1.10`` would read back as ``1.1``.
    """
    text = raw.strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if _INTEGER.fullmatch(text):
        return int(text)
    if text[:1] in ("[", '"', "'"):
        try:
            value = tomllib.loads(f"value = {text}")["value"]
        except tomllib.TOMLDecodeError:
            return text
        if isinstance(value, str | list):
            return value
    return text


def _kind(value: object) -> str:
    """The kind of value a setting holds, as a user would name it."""
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list):
        return 'list, like ["a", "b"]'
    return 'text (quote it if it looks like a number, like "007")'


def _core_sections() -> dict[str, set[str]]:
    return {name: set(type(model).model_fields) for name, model in Config()}


def _plugin_sections() -> set[str]:
    # Imports every plugin, so only for a section that isn't a core one
    from lestash.plugins.loader import load_plugins

    return set(load_plugins())


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
    core = _core_sections()
    if section in core:
        if setting not in core[section]:
            console.print(f"[red]Unknown key in {escape(f'[{section}]')}: {setting}[/red]")
            console.print(f"[dim]Known: {', '.join(sorted(core[section]))}[/dim]")
            raise typer.Exit(1)
        parsed: Any = value  # the model checks and converts it
    else:
        plugins = _plugin_sections()
        if section not in plugins:
            console.print(f"[red]Unknown section: {section}[/red]")
            console.print(f"[dim]Known: {', '.join(sorted(set(core) | plugins))}[/dim]")
            raise typer.Exit(1)
        parsed = parse_value(value)

    data = config.model_dump()
    current = data.get(section)
    if not isinstance(current, dict):
        current = data[section] = {}
    existing = current.get(setting)
    if isinstance(existing, dict):
        console.print(f"[red]{key} is a table of settings; edit it in {get_config_path()}[/red]")
        raise typer.Exit(1)
    # A plugin setting keeps its kind: "five" for a number, or a bare word for a list
    # (read as a list of its letters), would only fail at the next sync
    if section not in core and existing is not None and _kind(parsed) != _kind(existing):
        console.print(f"[red]{key} holds {escape(_kind(existing))}[/red]")
        raise typer.Exit(1)
    current[setting] = parsed

    # Save updated config
    try:
        updated_config = Config(**data)
        updated_config.save()
        stored = (
            getattr(getattr(updated_config, section), setting)
            if section in core
            else updated_config.get_plugin_config(section)[setting]
        )
        console.print(f"[green]Set {key} = {escape(repr(stored))}[/green]")
    except Exception as e:
        console.print(f"[red]Error: {escape(str(e))}[/red]")
        raise typer.Exit(1) from None
