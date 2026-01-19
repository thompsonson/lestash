"""Custom exceptions for Le Stash."""


class LeStashError(Exception):
    """Base exception for Le Stash."""


class ConfigError(LeStashError):
    """Configuration error."""


class DatabaseError(LeStashError):
    """Database error."""


class PluginError(LeStashError):
    """Plugin error."""


class SyncError(LeStashError):
    """Sync error."""
