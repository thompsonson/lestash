# lestash-kobo

Kobo eReader source plugin: books, highlights, notes, dog-ears and pen markups.

## How it works

1. `lestash kobo snapshot` copies the mounted Kobo (default `/media/kobo`) into a
   versioned store at `~/.local/share/lestash/kobo/`. File contents are stored once by
   SHA-256, so unchanged files cost nothing and no version is ever lost.
2. `lestash sources sync kobo` reads the newest snapshot's `KoboReader.sqlite`.

The Kobo is mounted read-only, so nothing writes to it.

## Install the plug-in sync

```bash
deploy/install.sh              # user services, no sudo
sudo deploy/install-root.sh    # once per machine: fstab line and udev rule
```

Then plug in the Kobo and tap Connect. udev starts `lestash-kobo.service`, which
mounts the Kobo, takes a snapshot, unmounts it, and starts the import.
`install-root.sh` is safe to re-run, and `install.sh` says when it is due.

## What becomes an item

- Each downloaded book, sideloaded file and Drive PDF; store titles you never opened
  are skipped unless `include_store_titles = true`.
- Each highlight, note, dog-ear and pen markup, as a child of its book.
  Markups carry the Kobo's JPG and SVG of the strokes.

A book that leaves the Kobo keeps its item, highlights and files (#314). The import
marks it `removed_from_device`: when the first snapshot imported without it was taken.
Its page reads "not on the Kobo since …". The mark goes if the book comes back.

Notebooks (#202), PDF ink (#200), reading activity (#201) and removed highlights
(#207) come later.

## Collections on the Kobo (#315)

You manage collections in LeStash; the Kobo shows the ones you switch on.

- Switch one on with **Show on Kobo** on its page, or `lestash kobo show NAME`.
- At each plug-in, after the snapshot, the unit runs `lestash kobo write-collections --mount`.
  - It plans the write on a copy of the snapshot's database, and leaves the Kobo alone when nothing changes.
  - It mounts `/media/kobo-rw`, checks the Kobo's database is the snapshot's copy, writes, runs `integrity_check`, and unmounts.
  - If the check fails, it puts the snapshot's copy back.
  - Each write is logged as `kobo-collections` in Settings → Sources.
- Only Kobo books go on: a collection's Kobo items, and items linked to one by `alternate_of`.
- A newly sideloaded book joins at the plug-in after the Kobo imports it.
- Collections made on the Kobo are never read or changed. A LeStash collection with the same name is left out, with an error.
- The Kobo keeps no order inside a collection; the reading order shows in LeStash only.


```bash
lestash kobo snapshot                                 # copy the mounted Kobo
lestash kobo snapshot --from DIR --taken-at ISO       # a saved copy, dated
lestash kobo snapshots                                # list snapshots
lestash sources sync kobo                             # import the newest snapshot
lestash kobo collections                              # each collection shown, and its status
lestash kobo show NAME / hide NAME                    # on or off the Kobo at the next plug-in
lestash kobo write-collections --dry-run              # what the next write would change
```

## Config (`~/.config/lestash/config.toml`)

```toml
[kobo]
# store_dir = "/path/to/kobo-store"   # default: kobo/ in the file store, files in its blobs/
include_store_titles = false                # default
```

A `store_dir` of its own keeps its snapshots' files there too; each book file and
markup image an import attaches is copied into the file store, which serves it.

Or set one from the command line: `lestash config set kobo.include_store_titles true`.
