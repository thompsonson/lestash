# Database backups

LeStash backs up its own database. sysbak, or any file backup, copies those backups and never the live database.

## Why not copy the live database

- `lestash.db` runs in WAL mode, so recent changes sit in `lestash.db-wal` until a checkpoint.
- A file copy takes `lestash.db`, `-wal` and `-shm` at different moments while LeStash writes.
- Such a copy can miss changes, or fail to open.
- SQLite's online backup API makes a consistent copy while the database is in use.

## The commands

| Command | Does |
|---|---|
| `lestash db backup create` | Copies the database, checks its integrity, and keeps it only if it passes |
| `lestash db backup create --label pre-deploy-355` | The same, with why in the file name |
| `lestash db backup create --keep 6` | The same, then deletes all but the newest 6 |
| `lestash db backup list` | Lists the backups, newest first |
| `lestash db backup verify [file]` | Checks a backup, the newest by default; exits 1 if it fails |
| `lestash db backup prune --keep N` | Deletes all but the newest N |

- Backups are `~/.local/share/lestash/backups/lestash-backup-<UTC time>[-<label>].db`.
- A copy is written as `….db.partial` and renamed once complete, so a half-made copy is never counted.
- A copy that fails its integrity check is deleted, and `create` exits 1 before it prunes.
- `--keep` and `prune` count every `lestash-backup-*.db`, labelled or not.
- Other files in the folder, made by hand, are never pruned; delete them yourself.

## The schedule

- `lestash-backup.timer` runs `lestash db backup create --keep 6` at 03:50, 07:50, 11:50, 15:50, 19:50 and 23:50.
- That is 10 minutes before each 4-hourly sysbak snapshot (00:00, 04:00 …).
- So the folder holds about a day of copies, and the sysbak drive holds weeks of them.
- `deploy/install.sh` installs and enables the timer.

## Before a change

- Make a labelled copy before a deploy, a migration or a data cleanup: `lestash db backup create --label pre-deploy-355`.
- Use the command, not `sqlite3 .backup` or `cp`, so the copy is named, checked and pruned.

## sysbak

- Back up `~/.local/share/lestash/backups/`.
- Exclude the live files. rsnapshot runs rsync with `--relative`, so write full paths:

```bash
exclude_patterns=(
    # … the defaults …
    "/home/mt/.local/share/lestash/lestash.db"
    "/home/mt/.local/share/lestash/lestash.db-wal"
    "/home/mt/.local/share/lestash/lestash.db-shm"
    "*.partial"
)
```

## Restore

1. Stop what writes: `systemctl --user stop lestash-server lestash-sync.timer lestash-backup.timer`.
2. Check the copy: `lestash db backup verify <file>`.
3. Keep the current database aside: `mv lestash.db lestash.db.before-restore`, and the same for `-wal` and `-shm`.
4. Copy the backup in: `cp backups/<file> lestash.db`.
5. Start again: `systemctl --user start lestash-server lestash-sync.timer lestash-backup.timer`.
