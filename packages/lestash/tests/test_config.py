"""Plugin sections in config.toml reach their plugins (#208)."""

from __future__ import annotations

from pathlib import Path

import pytest
import toml
from lestash.cli import config as config_cli
from lestash.core import config as core_config
from lestash.core.config import Config, as_bool
from lestash.plugins import loader
from typer.testing import CliRunner


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(core_config, "get_config_path", lambda: path)
    monkeypatch.setattr(config_cli, "get_config_path", lambda: path)
    monkeypatch.setattr(loader, "load_plugins", lambda: {"kobo": object(), "arxiv": object()})
    return path


def test_plugin_sections_are_kept_on_load_and_save(config_file: Path) -> None:
    config_file.write_text(
        '[general]\ndatabase_path = "/tmp/x.db"\n\n'
        '[kobo]\nstore_dir = "~/kobo"\ninclude_store_titles = true\n\n'
        '[arxiv]\nkeywords = ["ontology"]\n'
    )
    config = Config.load()
    assert config.get_plugin_config("kobo") == {
        "store_dir": "~/kobo",
        "include_store_titles": True,
    }
    assert config.get_plugin_config("arxiv") == {"keywords": ["ontology"]}

    config.save()
    saved = toml.load(config_file)
    assert saved["kobo"]["include_store_titles"] is True
    assert saved["general"]["database_path"] == "/tmp/x.db"


def test_a_missing_or_non_table_section_reads_as_empty(config_file: Path) -> None:
    config_file.write_text('stray = "x"\n\n[kobo]\nstore_dir = "~/kobo"\n')
    config = Config.load()
    assert config.get_plugin_config("stray") == {}
    assert config.get_plugin_config("bluesky") == {}
    assert config.get_plugin_config("general") == {}  # a core section, not a plugin's


def test_a_plugin_changing_its_config_does_not_change_the_config(config_file: Path) -> None:
    config_file.write_text('[arxiv]\nkeywords = ["ontology"]\n')
    config = Config.load()
    config.get_plugin_config("arxiv")["keywords"].append("changed")
    assert config.get_plugin_config("arxiv") == {"keywords": ["ontology"]}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("false", False),
        ("False", False),
        ("", False),
        (None, False),
        ("0", False),
        ("true", True),
        ("yes", True),
        (True, True),
        (False, False),
    ],
)
def test_as_bool_reads_old_string_switches(value: object, expected: bool) -> None:
    assert as_bool(value) is expected


def _set(*args: str):
    return CliRunner().invoke(config_cli.app, ["set", *args])


def test_config_set_stores_typed_plugin_values(config_file: Path) -> None:
    assert _set("kobo.include_store_titles", "false").exit_code == 0
    assert _set("kobo.store_dir", "~/kobo").exit_code == 0
    assert _set("arxiv.max_per_keyword", "5").exit_code == 0
    assert _set("arxiv.keywords", '["ontology", "agents"]').exit_code == 0

    config = Config.load()
    assert config.get_plugin_config("kobo") == {
        "include_store_titles": False,
        "store_dir": "~/kobo",
    }
    assert config.get_plugin_config("arxiv") == {
        "max_per_keyword": 5,
        "keywords": ["ontology", "agents"],
    }


def test_config_set_refuses_unknown_sections_and_core_keys(config_file: Path) -> None:
    result = _set("genral.database_path", "/tmp/x.db")
    assert result.exit_code == 1
    assert "Unknown section: genral" in result.output

    result = _set("general.databse_path", "/tmp/x.db")
    assert result.exit_code == 1
    assert "Unknown key in [general]: databse_path" in result.output

    assert not config_file.exists()  # nothing was saved


def test_config_set_keeps_other_sections(config_file: Path) -> None:
    config_file.write_text('[kobo]\nstore_dir = "~/kobo"\n')
    assert _set("general.database_path", "/tmp/x.db").exit_code == 0
    saved = toml.load(config_file)
    assert saved["kobo"] == {"store_dir": "~/kobo"}
    assert saved["general"]["database_path"] == "/tmp/x.db"


def test_config_show_lists_plugin_sections(config_file: Path) -> None:
    config_file.write_text('[kobo]\nstore_dir = "~/kobo"\n')
    result = CliRunner().invoke(config_cli.app, ["show"])
    assert result.exit_code == 0
    assert "[kobo]" in result.output
    assert "store_dir = ~/kobo" in result.output
