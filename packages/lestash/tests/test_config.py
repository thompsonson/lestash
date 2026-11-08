"""Plugin sections in config.toml reach their plugins (#208)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
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
    plugins = {"kobo": object(), "arxiv": object(), "linkedin": object()}
    monkeypatch.setattr(loader, "load_plugins", lambda: plugins)
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
    saved = tomllib.loads(config_file.read_text())
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("false", False),
        ("True", True),
        ("FALSE", False),
        ("5", 5),
        ("0", 0),
        ("-3", -3),
        ("007", "007"),  # an ID keeps its leading zeros
        ("+4412345", "+4412345"),
        ('["LLM agents", "ontology"]', ["LLM agents", "ontology"]),
        ('"007"', "007"),
        ("LLM agents", "LLM agents"),
        ("1.10", "1.10"),  # not 1.1
        ("0.5", 0.5),
        ("-2.25", -2.25),
        ("inf", "inf"),
        ("2026-01-01", "2026-01-01"),
        ("~/kobo", "~/kobo"),
        ("[agents", "[agents"),  # not valid TOML: kept as text
    ],
)
def test_parse_value_types_only_unambiguous_text(raw: str, expected: object) -> None:
    value = config_cli.parse_value(raw)
    assert value == expected and type(value) is type(expected)


def test_config_set_refuses_a_bare_word_for_a_list_setting(config_file: Path) -> None:
    config_file.write_text('[arxiv]\nkeywords = ["ontology"]\n')
    result = _set("arxiv.keywords", "LLM agents")
    assert result.exit_code == 1
    assert 'holds list, like ["a", "b"]' in result.output
    assert Config.load().get_plugin_config("arxiv") == {"keywords": ["ontology"]}


def test_config_set_refuses_text_for_a_number_setting(config_file: Path) -> None:
    config_file.write_text("[arxiv]\nmax_per_keyword = 5\n")
    result = _set("arxiv.max_per_keyword", "five")
    assert result.exit_code == 1
    assert "holds number" in result.output
    assert _set("arxiv.max_per_keyword", "8").exit_code == 0


def test_config_set_replaces_a_value_an_older_version_stored_as_text(config_file: Path) -> None:
    config_file.write_text('[kobo]\ninclude_store_titles = "false"\n\n[arxiv]\nlimit = "100"\n')
    assert _set("kobo.include_store_titles", "true").exit_code == 0
    assert _set("arxiv.limit", "100").exit_code == 0
    assert Config.load().get_plugin_config("kobo") == {"include_store_titles": True}
    assert Config.load().get_plugin_config("arxiv") == {"limit": 100}


def test_config_set_takes_a_decimal_for_a_number_setting(config_file: Path) -> None:
    config_file.write_text("[arxiv]\nmin_size_kb = 0.5\n")
    assert _set("arxiv.min_size_kb", "1.5").exit_code == 0
    assert _set("arxiv.min_size_kb", "2").exit_code == 0
    assert Config.load().get_plugin_config("arxiv") == {"min_size_kb": 2}


def test_config_set_trims_text_and_reports_the_stored_value(config_file: Path) -> None:
    result = _set("kobo.store_dir", " ~/kobo ")
    assert "Set kobo.store_dir = '~/kobo'" in result.output
    assert Config.load().get_plugin_config("kobo") == {"store_dir": "~/kobo"}
    result = _set("logging.console_enabled", "false")
    assert "Set logging.console_enabled = False" in result.output  # the model's bool


def test_save_keeps_a_symlinked_config_and_its_permissions(
    config_file: Path, tmp_path: Path
) -> None:
    target = tmp_path / "dotfiles" / "config.toml"
    target.parent.mkdir()
    target.write_text('[kobo]\nstore_dir = "~/kobo"\n')
    target.chmod(0o600)
    config_file.symlink_to(target)

    assert _set("kobo.include_store_titles", "true").exit_code == 0
    assert config_file.is_symlink()
    assert (target.stat().st_mode & 0o777) == 0o600
    assert "include_store_titles = true" in target.read_text()
    assert [p.name for p in target.parent.iterdir()] == ["config.toml"]  # no temp file left


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Users\x",  # the old toml library wrote \U and \x unescaped
        r"^\x20+$",
        "x\x7f",  # and mangled control characters
        ["agents", 2026],  # and couldn't write lists mixing kinds
    ],
)
def test_save_round_trips_values_the_old_toml_library_broke(
    config_file: Path, value: object
) -> None:
    Config(kobo={"value": value}).save()
    assert Config.load().get_plugin_config("kobo") == {"value": value}


def test_save_refuses_a_config_that_would_not_load_again(config_file: Path) -> None:
    config_file.write_text('[kobo]\nstore_dir = "~/kobo"\n')
    before = config_file.read_text()
    with pytest.raises(ValueError, match="would not load again"):
        Config(kobo={"value": None}).save()  # TOML has no null
    assert config_file.read_text() == before


def test_config_set_refuses_to_replace_a_table(config_file: Path) -> None:
    config_file.write_text("[linkedin.feed_preview]\nenabled = true\n")
    result = _set("linkedin.feed_preview", "false")
    assert result.exit_code == 1
    assert "is a table of settings" in result.output
    assert Config.load().get_plugin_config("linkedin") == {"feed_preview": {"enabled": True}}


def test_config_set_prints_brackets_in_values(config_file: Path) -> None:
    result = _set("arxiv.keywords", '["agents"]')
    assert result.exit_code == 0
    assert "Set arxiv.keywords = ['agents']" in result.output


def test_a_core_key_is_set_without_loading_plugins(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_plugins() -> dict:
        raise AssertionError("plugins loaded for a core key")

    monkeypatch.setattr(loader, "load_plugins", no_plugins)
    assert _set("logging.level", "DEBUG").exit_code == 0
    assert Config.load().logging.level == "DEBUG"
    # The model checks a core value
    assert _set("logging.level", "LOUD").exit_code == 1


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
    saved = tomllib.loads(config_file.read_text())
    assert saved["kobo"] == {"store_dir": "~/kobo"}
    assert saved["general"]["database_path"] == "/tmp/x.db"


def test_config_show_lists_plugin_sections(config_file: Path) -> None:
    config_file.write_text('[kobo]\nstore_dir = "~/kobo"\n')
    result = CliRunner().invoke(config_cli.app, ["show"])
    assert result.exit_code == 0
    assert "[kobo]" in result.output
    assert "store_dir = ~/kobo" in result.output
