# Text from files

W3 (#218): searchable text for files that have none.

## Step 1: NotebookLM slides, infographics and apps

- `lestash enrich text` reads each file not read yet, then rebuilds the text of the items that show it.
- The timer sync starts it afterwards (`lestash-extract.service`, at low priority), whether or not every source synced. A run stops reading after an hour; the next one goes on.
- Slides and infographics are pictures. RapidOCR's English model (PP-OCRv5) reads them, about 0.7 s a picture on pop-mini's CPU. Docling, tried first, dropped most of a slide's text as a picture, and RapidOCR's default Chinese model ran English words together.
- Apps are web pages: their visible text is kept; scripts and styles are skipped, and nothing is run.
- Audio and video come in step 2 (faster-whisper).

## Where the text goes

- `blob_text` (migration 23) keeps each file's text once, by its hash and the tool that read it, with the tool's version.
- An item's `content` is its own text, then `## Text from the files`, then `## Slide 7` and its text for each slide, or the text of its picture or page.
- `metadata.text_from` names the tool versions that read it.
- The part is rebuilt from `blob_text` each time the item is stored, so an import keeps it and writes no history. A change to the text is kept in `item_history`, tagged `extract`.
- Keyword search finds a phrase on a slide; the snippet shows the text near it.
