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
- A PDF or EPUB on the Kobo that its database doesn't list, as its book. The Kobo can
  show a sideloaded PDF, and save pen notes into it, without writing a row for it. Its
  title is the PDF's own, else the file name; it is marked `no_kobo_row`. Each version
  of the file, such as one with your pen notes, is a version of its media.

A book that leaves the Kobo keeps its item, highlights and files (#314). The import
marks it `removed_from_device`: when the first snapshot imported without it was taken.
Its page reads "not on the Kobo since …". The mark goes if the book comes back.

Notebooks (#202), the text under PDF ink (#200), reading activity (#201) and removed highlights
(#207) come later.

## Collections on the Kobo (#315)

You manage collections in LeStash; the Kobo shows the ones you switch on.

- Switch one on with **Show on Kobo** on its page, or `lestash kobo show NAME`.
- At each plug-in, after the snapshot, the unit runs `lestash kobo write-collections --mount`.
  - It plans the write on a copy of the snapshot's database, and leaves the Kobo alone when nothing changes.
  - It mounts `/media/kobo-rw`, checks the Kobo's database is the snapshot's copy, writes, runs `integrity_check`, and unmounts.
  - It refuses to write when the database looks unsaved: a book file it doesn't list, or a PDF changed after its newest reading time and after the last eject.
  - If the check fails, it puts the snapshot's copy back.
  - It refuses to write when the snapshot's database fails `integrity_check`, and shows the damage as the collection's error (#339).
  - Each write is logged as `kobo-collections` in Settings → Sources.
- Only Kobo books go on: a collection's Kobo items, and items linked to one by `alternate_of`.
- A newly sideloaded book joins at the plug-in after the Kobo imports it.
- Collections made on the Kobo are never read or changed. A LeStash collection with the same name is left out, with an error.
- The Kobo keeps no order inside a collection; the reading order shows in LeStash only.
- Last, the unit ejects the Kobo (`lestash kobo eject`), so it leaves USB mode.
  - The Kobo saves its database, and imports new books, only when it leaves USB mode.
  - An unmount alone left it on its Connected screen for 30 hours, with nothing saved.
  - Ejecting without root needs the polkit grant from `sudo deploy/install-root.sh`.
  - To keep the Kobo connected, create `~/.config/lestash/kobo-no-eject`.
- To pause collection writes, create `~/.config/lestash/kobo-writes-paused`; snapshots and imports still run.


```bash
lestash kobo snapshot                                 # copy the mounted Kobo
lestash kobo snapshot --from DIR --taken-at ISO       # a saved copy, dated
lestash kobo snapshots                                # list snapshots
lestash sources sync kobo                             # import the newest snapshot
lestash kobo collections                              # each collection shown, and its status
lestash kobo show NAME / hide NAME                    # on or off the Kobo at the next plug-in
lestash kobo write-collections --dry-run              # what the next write would change
lestash kobo eject                                    # eject, so the Kobo leaves USB mode
```

## PDFs to the Kobo (#338)

- Ask for an item on the Kobo with `lestash kobo send ID`, or `PUT /api/items/{id}/kobo`. Every item in a collection shown on the Kobo is asked for too.
- `lestash kobo unsend ID` stops asking. A copy already on the Kobo stays.
- `lestash kobo files` lists what is asked for, and where each item stands.
- At each plug-in, after the collections are written, the unit runs `lestash kobo copy-files --mount`.
  - It copies the newest version of each item asked for into `LeStash/`, named after its title.
  - It never overwrites or deletes a file. A taken name gets the file's hash added: `Title (1a2b3c4d).pdf`.
  - It reads each copy back and checks its SHA-256 before recording it.
  - It skips a file whose bytes are already on the Kobo, however they got there.
  - Items without a PDF, and books taken off the Kobo, are left out.
  - Each run is logged as `kobo-files` in Settings → Sources.
- The Kobo imports the copies when it is ejected. The next plug-in's import links each book to its item by its bytes (`alternate_of`), and collections show it from then on.
  - The link holds after you draw on the copy, because the book keeps the copied bytes as its first version.
- So a new PDF takes two plug-ins to show in a collection on the Kobo: one to copy it, one to write the collection.

## Notes drawn on PDFs (#338)

- The Elipsa saves pen notes into the PDF itself. Each snapshot where a PDF changed is an annotated state, kept as a version of the book's file.
- After each import, the import unit runs `lestash kobo extract-notes`. It reads each book's states not read yet, oldest first.
  - Each note becomes a child of the book, matched across states by its strokes, so a later state adds only new ones.
  - A note erased later is kept, marked `removed_from_pdf`.
  - Only states newer than the newest one read are applied, so a state read late can't mark newer notes erased. A file put back to earlier bytes is a new state.
  - A state whose file is gone from the store is passed over and reported. It is read once its file is back, unless a newer state was read first.
  - A state with new notes records an `annotated` activity at the time the state was first seen, naming its new notes.
- Each state read is recorded per book and by extractor version, with the notes first read from it (`blob_text`, tool `pdf-annotations:item-N`).
  - After a newer extractor, each book's newest state read is read again, then any newer ones.
  - A PDF PyMuPDF can't read is recorded and not retried. A file it can't open is tried again at the next run.
- Each run is logged as `kobo-notes` in Settings → Sources.
- The text under a note, and reading handwriting, come later (#200).

## Config (`~/.config/lestash/config.toml`)

```toml
[kobo]
# store_dir = "/path/to/kobo-store"   # default: kobo/ in the file store, files in its blobs/
include_store_titles = false                # default
```

A `store_dir` of its own keeps its snapshots' files there too; each book file and
markup image an import attaches is copied into the file store, which serves it.

Or set one from the command line: `lestash config set kobo.include_store_titles true`.
