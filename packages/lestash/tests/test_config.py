"""Config keeps plugin sections."""

import toml
from lestash.core import config as config_mod
from lestash.core.config import Config


def test_plugin_sections_are_returned() -> None:
    config = Config(**{"kobo": {"include_store_titles": True}})
    assert config.get_plugin_config("kobo") == {"include_store_titles": True}
    assert config.get_plugin_config("missing") == {}


def test_plugin_sections_survive_load_and_save(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[kobo]\nstore_dir = "/tmp/kobo"\n')
    monkeypatch.setattr(config_mod, "get_config_path", lambda: path)
    config = Config.load()
    assert config.get_plugin_config("kobo") == {"store_dir": "/tmp/kobo"}
    config.save()
    assert toml.load(path)["kobo"] == {"store_dir": "/tmp/kobo"}
