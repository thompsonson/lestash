# Text from files

W3 (#218): searchable text for files that have none.

## Step 1: NotebookLM slides, infographics and apps

- `lestash enrich text` reads each file not read yet, then rebuilds the text of the items that show it.
- The timer sync starts it afterwards (`lestash-extract.service`, at low priority), whether or not every source synced. A run stops reading after an hour; the next one goes on.
- Slides and infographics are pictures. RapidOCR's English model (PP-OCRv5) reads them, about 0.7 s a picture on pop-mini's CPU. Docling, tried first, dropped most of a slide's text as a picture, and RapidOCR's default Chinese model ran English words together.
- Apps are web pages drawn by scripts from data in their `data-app-data` attribute: the texts in that data (flashcards, mind maps) and the page's visible text are kept; scripts and styles are skipped, and nothing is run.
- Audio and video come in step 2 (faster-whisper).

## Where the text goes

- `blob_text` (migration 23) keeps each file's text once, by its hash and the tool that read it, with the tool's version.
- An item's `content` is its own text, then `## Text from the files`, then `## Slide 7` and its text for each slide, or the text of its picture or page.
- `metadata.text_from` names the tool versions that read it.
- The part is rebuilt from `blob_text` each time the item is stored, so an import keeps it and writes no history. A change to the text is kept in `item_history`, tagged `extract`.
- Keyword search finds a phrase on a slide; the snippet shows the text near it.
- A deck made again keeps its old slides as media; only the newest file at each position is read into the text.
- A file that can't be read is recorded with no text, and read again once the tool's version changes, or with `--retry-failed`. A run whose OCR engine can't start, or in which a tool fails on three or more files, reads none, and can't read again a file it read before, records none of those failures and fails the unit; a busy database stops a run, and the next one goes on. A file missing from the store is skipped.
- RapidOCR fetches its English model on first use, into its package folder, so the first run after an install needs the network.
- The part under `## Text from the files` is rebuilt: an edit made inside it is lost.
- Not handled: a deck made again with fewer slides keeps its old last slides' text, and an older export imported after a newer one wins at each position. Exports arrive in order, and NotebookLM gives a remade deck its own artifact, so neither has happened yet.
