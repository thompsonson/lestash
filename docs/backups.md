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
| `lestash db backup create` | Copies the database, compresses it, reads it back to check its integrity, and keeps it only if it passes |
| `lestash db backup create --label pre-deploy-355` | The same, with why in the file name |
| `lestash db backup create --keep 3` | The same, then deletes all but the newest 3 scheduled (unlabelled) backups |
| `lestash db backup list` | Lists the backups, newest first |
| `lestash db backup verify [file]` | Checks a backup, the newest by default; exits 1 if it fails |
| `lestash db backup prune --keep N` | Deletes all but the newest N scheduled backups |
| `lestash db backup prune --labelled --keep N` | Deletes all but the newest N labelled backups |
| `lestash db backup extract <file> --to <path>` | Writes the database a backup holds to `<path>`, decompressed; never over a file |
| `lestash db backup compress` | Compresses the backups made before compression, each checked before and after |

- Backups are `~/.local/share/lestash/backups/lestash-backup-<UTC time>[-<label>].db.zst`.
- They are compressed with zstd (#414): a 1.34 GB database became 0.41 GB, in under 3 s.
- Each carries a checksum, so a damaged file fails to decompress rather than give wrong data.
- If `verify` can't write its temporary copy, say for want of space, the backup is reported unchecked, not invalid; `create` keeps it, exits 1 and does not prune.
- A plain `.db` backup with a non-empty `-wal`, or a `-journal`, beside it is left as it is by `compress` and `extract`.
- `extract` writes `<path>.<id>.partial` first. If it is killed, that file stays: delete it by hand.
- Backups made before #414 end in `.db`. They are listed, checked and pruned the same way; `compress` converts them.
- `verify` decompresses a `.zst` backup into a temporary `….verify.<id>.partial` beside it, checks that, and removes it. So a pass shows the backup can be restored. It needs free space for one uncompressed copy while it runs.
- A copy is one file: no `-wal` or `-shm` beside it.
- A copy is written as `….db.partial`, compressed to `….db.zst.partial`, and moved into place once complete, so a half-made copy is never counted.
- A `.partial` over an hour old was cut short; the next `create` removes it.
- `create` refuses to replace a backup of the same name, made in the same second.
- `verify` opens a backup read-only, and reports a missing file without creating one.
- A copy that fails its integrity check is deleted, and `create` exits 1 before it prunes.
- Scheduled (unlabelled) and labelled backups are pruned apart: the schedule's `--keep` never deletes a labelled copy.
- Labelled copies stay until `prune --labelled` removes them; the sysbak drive holds them for weeks after.
- Only `lestash-backup-<time>[-<label>].db` and the same names ending `.db.zst` are pruned. A file under any other name, such as one made by hand, never is; delete it yourself.

## The schedule

- `lestash-backup.timer` runs `lestash db backup create --keep 3` daily at 03:50.
- That is 10 minutes before sysbak's 04:00 snapshot, which takes the new copy.
- So the folder holds three days of copies, and the sysbak drive holds weeks of them.
- Each copy is a new file that snapshots cannot share with older ones.
- A 4-hourly copy used about six times the space, on this disk and on the sysbak drive.
- The cost is that a failure can lose up to a day of changes.
- Synced data can be fetched again; notes, shares and Kobo notes since 03:50 cannot.
- Make a labelled copy before any risky change during the day.
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

1. Stop everything that writes:
   `systemctl --user stop lestash-server lestash-sync.timer lestash-backup.timer lestash-sync lestash-extract lestash-embed lestash-backup lestash-kobo 'lestash-kobo-sync@*'`.
2. Check nothing still has the database open: `fuser ~/.local/share/lestash/lestash.db*` prints nothing.
3. Check the copy: `lestash db backup verify <file>`.
4. Write it out beside the database: `lestash db backup extract <file> --to ~/.local/share/lestash/lestash.db.restore`.
   It decompresses a `.zst` backup, and never writes over a file.
   Do this before step 5: any `lestash` command makes an empty `lestash.db` when there is none.
5. Keep the current database aside: `mv lestash.db lestash.db.before-restore`, and the same for `-wal` and `-shm`.
6. Put the backup in its place: `mv lestash.db.restore lestash.db`.
7. Start again: `systemctl --user start lestash-server lestash-sync.timer lestash-backup.timer`.
   The Kobo services start on the next plug-in.
