"""Objects a source plugin can yield from ``sync()`` besides items."""

from typing import Any

from pydantic import BaseModel


class SyncCheckpoint(BaseModel):
    """Plugin state saved together with the items yielded before it.

    The sync loop stores the checkpoint in ``sync_checkpoints`` and commits it with those
    items (it may have committed some of them earlier), so the state never claims work
    whose items were lost. A
    plugin reads its checkpoints back with ``database.get_checkpoints(conn, plugin.name)``.
    ``value=None`` deletes the checkpoint.
    """

    key: str
    value: dict[str, Any] | None
