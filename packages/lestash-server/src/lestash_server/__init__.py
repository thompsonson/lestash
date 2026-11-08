"""Le Stash API Server."""

from importlib.metadata import PackageNotFoundError, version

# The release bumps pyproject.toml; read it from the installed package
try:
    __version__ = version("lestash-server")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0+unknown"
