# Where LeStash keeps its files

LeStash follows the XDG layout (#304). One module names every folder: `packages/lestash/src/lestash/core/paths.py`.

## Folders

| Folder | Variable | Holds | Backed up |
|---|---|---|---|
| `~/.config/lestash` | `XDG_CONFIG_HOME` | `config.toml`, client secrets, OAuth tokens, `jelu.json`, `gmail.json`, the Kobo switch files | yes |
| `~/.local/share/lestash` | `XDG_DATA_HOME` | `lestash.db`, the file store (`blobs/`), the Kobo store (`kobo/`), `media/` | yes, but not the live `lestash.db`, `-wal` or `-shm`: see [backups](backups.md) |
| `~/.local/share/lestash/backups` | | LeStash's own consistent copies of the database: [backups](backups.md) | yes: the database is backed up through these |
| `~/.cache/lestash` | `XDG_CACHE_HOME` | Drive downloads (`downloads/`, Takeout zips too), `drive/`, `voice/`, `posts/`, speech models | no |
| `~/.local/state/lestash` | `XDG_STATE_HOME` | `logs/`, sync `locks/`, `bluesky_session.json` | no |

- A variable that isn't an absolute path is ignored, as the XDG spec says.
- Takeout zips are cache: they stay on Drive, so they can be downloaded again.

## Before the move

Before #304 everything lived in `~/.config/lestash`, the legacy folder.

- A file still in the legacy folder is used in place of the new one.
- The legacy file wins even when both exist. A file in the new place may be an empty leftover, such as pop-mini's 0-byte `~/.local/share/lestash/lestash.db`.
- After the one-time move the legacy files are gone, so the new places are used.

## Where a setting comes from

After the Twelve-Factor App ([III, Config](https://12factor.net/config); [IV, Backing services](https://12factor.net/backing-services)), a setting comes from, in order:

1. its environment variable;
2. `config.toml`;
3. the default above.

| Variable | Setting |
|---|---|
| `LESTASH_DATABASE_PATH` | `general.database_path` |
| `LESTASH_STORE_PATH` | `general.store_path` |
| `LESTASH_LOG_FILE` | `logging.file_path` |

- A value from the environment is never saved to `config.toml`.
- `config.toml` holds only what you set. Saving never writes a default, so the file can't pin an old place.
- The server's own variables (`LESTASH_HOST`, `LESTASH_PORT`, `LESTASH_TLS_CERT`, `LESTASH_TLS_KEY`, `LESTASH_STATIC_DIR`) are unchanged.

## Places that follow the database

The media folder, file store, post cache, sync locks and backups depend on the database:

- **The default database:** each comes from `lestash.core.paths`.
- **A `database_path` other than the default:** each sits beside that database (`media/`, `store/`, `cache/posts/`, `<db>.sync-<source>.lock`, backups), so a second database, such as a test's, never shares them.
- `general.store_path` overrides the store's folder in both cases.
