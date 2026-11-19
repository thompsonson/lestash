# Text from files

W3 (#218): searchable text for files that have none.

## Step 1: NotebookLM slides, infographics and apps

- `lestash enrich text` reads each file not read yet, then rebuilds the text of the items that show it.
- The timer sync starts it afterwards (`lestash-extract.service`, at low priority), whether or not every source synced. A run stops reading after an hour; the next one goes on.
- Slides and infographics are pictures. RapidOCR's English model (PP-OCRv5) reads them, about 0.7 s a picture on pop-mini's CPU. Docling, tried first, dropped most of a slide's text as a picture, and RapidOCR's default Chinese model ran English words together.
- Apps are web pages drawn by scripts from data in their `data-app-data` attribute: the texts in that data (flashcards, mind maps) and the page's visible text are kept; scripts and styles are skipped, and nothing is run.

## Step 2: NotebookLM audio overviews and explainer videos

- `lestash.core.transcribe_worker` makes a recording's words and speakers. faster-whisper's `small.en` model hears the words, with each word's time. sherpa-onnx finds when each voice speaks.
- A two-host overview reads as turns: `**Speaker 1** (2:14): ...`. Speakers are numbered as they first speak.
- One voice reads as text with a `## 12:34` heading about each minute. Briefs (`SHOW_FORMAT_THE_BRIEF`) and explainer videos have one voice.
- An overview with no format recorded is taken to have two hosts: left to count them, the worker heard six voices in its first 17 seconds.
- sherpa-onnx's CAM++ English voice model tells the two hosts apart; TitaNet small put both hosts of 3 overviews in 4 under one speaker.
- Each sentence goes to one voice: the one heard for most of its words (#381).
- A diarizer turn's edge often falls a word or two off, and a short blip of the other voice can sit inside a turn; given a voice word by word, phrases were split between speakers.
- A sentence keeps two voices only when each speaks at least 16 words of it: whisper ran two sentences together without a full stop.
- Over the 25 overviews stored before the change, half the speaker changes fell mid-sentence; grouped by sentence, 9 in 1,836 do.
- A capitalised title ("Dr.", "Ms."), "e.g.", "i.e." and "vs." never end a sentence.
- "Fig." and "approx." don't end a sentence before a number.
- Another abbreviation, an initial or an acronym ("Main St.", "Inc.", "200 ms.", "B.", "U.S.") ends one when the next word starts with a capital. It does not when the next word is lowercase.
- Limit: text alone can't tell "Baker St. Right." from "St. Louis", or "Plan B. Then" from "John B. Watson". These are cut at the stop. A wrong cut matters only if a voice change falls there; a wrong join would give a reply to the wrong voice.
- `lestash enrich regroup-transcripts` groups stored transcripts again from their text, with no recording heard again. `--dry-run` only counts.
- A regrouped turn starting with words that moved forward into the next stored turn takes that turn's start: never before the real start, and late by at most those words.
- A turn left after its head moved back keeps its stored start.
- `base.en` is three times faster than `small.en` but mishears more ("a server out of you" for "a server outage"). Whisper's voice-activity filter skips music and silence, where it otherwise makes up words.
- ffmpeg turns each recording into 16 kHz mono audio first.

### Where it runs

- With no `[transcribe]` table, the worker runs here: one recording at a time, on 4 threads. Its models go to `~/.cache/lestash/whisper-models` (the folder `lestash-voice` uses) and `~/.cache/lestash/speaker-models`; until the move (#304), the old folders in `~/.local/share/lestash`.
- With a host set, recordings go there in batches over ssh:

  ```toml
  [transcribe]
  host = "computer"  # an ssh name; key login, no password
  workers = 4        # recordings at once (default 4 on a host, 1 here)
  threads = 6        # threads each (default 6 on a host, 4 here)
  ```

- The host keeps the worker in `~/lestash-worker`: its own Python environment, with the packages pinned, and the models (about 1 GB). It needs `python3`, `uv`, `rsync` and a user systemd.
- Each batch is copied to `~/lestash-worker/jobs/<id>`, run at `nice` 10 with a CPU and memory cap (`workers × threads` CPUs, 3 GB a worker), and deleted once its results are back.
- In a trial on computer, one worker on 6 threads ran at about 10 times real time.
- If the host can't be reached, the recordings wait for the next run; pictures and pages are read as usual.
- Two recordings a worker go between time-budget checks, so a batch started before the hour is up is finished.
- A tool that seems broken is checked by reading again the smallest file it read before.

## Where the text goes

- `blob_text` (migration 23) keeps each file's text once, by its hash and the tool that read it, with the tool's version.
- An item's `content` is its own text, then `## Text from the files`, then `## Slide 7` and its text for each slide, or the text of its picture, page or recording.
- `metadata.text_from` names the tool versions that read it.
- The part is rebuilt from `blob_text` each time the item is stored, so an import keeps it and writes no history. A change to the text is kept in `item_history`, tagged `extract`.
- Keyword search finds a phrase on a slide; the snippet shows the text near it.
- A deck made again keeps its old slides as media; only the newest file at each position is read into the text.
- A file that can't be read is recorded with no text, and read again once the tool's version changes, or with `--retry-failed`. A run whose OCR or transcription engine can't start, or in which a tool fails on three or more files, reads none, and can't read again a file it read before, records none of those failures and fails the unit; a busy database stops a run, and the next one goes on. A file missing from the store is skipped.
- RapidOCR fetches its English model on first use, into its package folder, so the first run after an install needs the network.
- The part under `## Text from the files` is rebuilt: an edit made inside it is lost.
- Not handled: a deck made again with fewer slides keeps its old last slides' text, and an older export imported after a newer one wins at each position. Exports arrive in order, and NotebookLM gives a remade deck its own artifact, so neither has happened yet.
