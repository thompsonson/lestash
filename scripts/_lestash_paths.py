"""``lestash.core.paths`` for the scripts here, loaded by file (#304).

The scripts run with a plain ``python3`` (their shebang) as well as ``uv run``. Importing
``lestash.core.paths`` as a package would load ``lestash.core``, which needs the
workspace's dependencies. ``paths.py`` needs only the standard library, so it is loaded
on its own: the scripts get their folders from the one paths module all the same.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_SOURCE = Path(__file__).resolve().parents[1] / "packages/lestash/src/lestash/core/paths.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_lestash_core_paths", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


paths = _load()
