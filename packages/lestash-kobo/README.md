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

Notebooks (#202), PDF ink (#200) and reading activity (#201) come later.

## Commands

```bash
lestash kobo snapshot                                 # copy the mounted Kobo
lestash kobo snapshot --from DIR --taken-at ISO       # a saved copy, dated
lestash kobo snapshots                                # list snapshots
lestash sources sync kobo                             # import the newest snapshot
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
