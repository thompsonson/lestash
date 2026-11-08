"""The test environment (lestash.testing): HOME is a temp folder, and a test that opens
the default database fails."""

import os
import subprocess
import sys
from pathlib import Path

LEAKS = """
from lestash.core.database import get_connection

def test_uses_the_default_config():
    with get_connection() as conn:  # no config: Config.load(), the default database
        conn.execute("SELECT 1")
"""


def test_home_is_a_temp_folder() -> None:
    assert Path.home() == Path(os.environ["HOME"])
    assert not (Path.home() / ".config" / "lestash").exists()


def test_a_test_that_opens_the_default_database_fails(tmp_path: Path) -> None:
    test_file = tmp_path / "test_leak.py"
    test_file.write_text(LEAKS)
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_file)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 1, run.stdout
    assert "The test opened the default database" in run.stdout
