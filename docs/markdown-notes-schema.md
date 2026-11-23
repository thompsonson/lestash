# Markdown Notes: Frontmatter Schema

*Proposed 2026-11-23, version 1. For the markdown notes source planned in #408.*

## Overview

LeStash can import a folder of markdown notes kept in a git repository.
This document says what it reads from each note, and how each field becomes part of an item.

- The schema is the contract between a notes repository and LeStash.
- A repository can adopt it directly, or a reformatter outside LeStash can map its own conventions onto it.
- Every field is optional. A note with no frontmatter still imports, using the fallbacks below.
- Fields LeStash doesn't know are kept in the item's metadata, uninterpreted.
- Terms reuse the ontology's item kinds and typed links ([content-and-activity-ontology-design.md](content-and-activity-ontology-design.md)).
- Two JSON Schemas state the same rules, so a repository can check its notes:
  - [`schemas/markdown-notes.schema.json`](schemas/markdown-notes.schema.json) for one note's fields;
  - [`schemas/markdown-notes-sidecar.schema.json`](schemas/markdown-notes-sidecar.schema.json) for the sidecar file.

## Where the frontmatter comes from

LeStash reads a note's fields from two places, in this order:

1. **The note's own YAML frontmatter**, between `---` lines at the top of the file.
2. **A sidecar file**, `.lestash/notes.json` at the folder's root, keyed by each note's path.
   - It holds fields for notes that should not be edited, such as historic or verbatim ones.
   - A sidecar entry replaces the note's own value field by field, at the top level only.
     A list or map in the sidecar, such as `tags` or `labels`, replaces the note's whole value; nothing is merged.
   - A reformatter can generate it, so the notes themselves stay untouched.

```json
{
  "schema": 1,
  "notes": {
    "meetings/2026-03_kickoff.md": { "note_class": "record-of-exchange", "labels": { "project": ["alpha"] } }
  }
}
```

### When frontmatter doesn't parse, or breaks the schema

The frontmatter block is never part of the body, so an unparsed block never becomes the summary.

**A note's frontmatter isn't valid YAML** (a common cause is an unquoted value holding `": "`):

- With a sidecar entry, the note imports with the sidecar's fields alone. The sidecar can carry the fields until the note is fixed.
- Without one, the note is held back: not imported, and an existing item from it is left as it was.
  It can't be imported safely, because its unread fields may say `lestash: skip`, or mark it a capture of someone else's work.

**A field parses but breaks the schema:**

- `lestash`, `own`, `author` or `capture` broken: the note is held back, as above. These fields decide whether and as whose work it is imported.
- Any other field broken, such as `note_class: model`: that field is ignored, and the note's other fields still apply.

**The sidecar isn't valid JSON, or breaks the sidecar schema at its top level:**

- The source's sync stops before changing anything, because the sidecar may hold skips and captures.
- One entry with a broken field is handled field by field, by the same rules as frontmatter.

Every held-back note, ignored field and stopped sync is listed, with the reason, in the source's sync report.

## The fields

### Identity and dates

| Field | Type | Becomes | Fallback when absent |
|---|---|---|---|
| `title` | string | the item's title (`dcterms:title`) | the first `#` heading, then the filename |
| `date` | `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, or an ISO 8601 date-time | when it was written (`dcterms:created`); the precision is kept | a date in the filename, then the note's first git commit |
| `summary` | string, one or two sentences | the preview on cards | the first paragraph |
| `medium` | `typed`, `voice`, `conversation`, `correspondence`, `mixed` | how the note was made, as the label `medium:<value>` | none |
| `own` | boolean | whether you wrote it (`is_own_content`) | true, or false under `capture` |
| `author` | string | the item's author | the repository's owner when `own` is true; none when `own` is false |

- Every item from this source has the kind `note`; the ontology derives kind from the source.
- A filename date is either `YYYY-MM-DD` at the start, or a month and year at the end: `_nov2026`.
- Quote every date, wherever it appears: `date: "2026"`, `"2026-11"`, `"2026-11-09"`.
  Unquoted, YAML reads a year as a number and a full date as a date object, and the JSON Schema rejects both.
  LeStash itself accepts either form.
- A date given to the month is shown as a month, such as "Nov 2026". LeStash never invents a day.
- The git commit date is the weakest sign: a bulk move or a rename resets it.

### How to read it

| Field | Values | Shown as |
|---|---|---|
| `note_class` | `record-of-exchange`, `live-model`, `proposal` | the label `note_class:<value>`, and a banner on the item's page |
| `status` | a short phrase, such as `draft`, `open` or `confirmed` | the label `status:<value>` |
| `type` | the repository's own word for the note, such as `review` or `position` | the label `type:<value>`, as written |
| `scope` | a phrase: what the note covers | a line under the title |
| `amended` | a list; each entry is `{date, summary}`, or a string `"YYYY-MM-DD — summary"` | "Amended N times, last on …" on the item's page |
| `superseded_by` | a reference (see below) | a banner linking the newer note |

The three classes say how far to trust a note today:

- **`record-of-exchange`**: correct as of its date, and not maintained since. A meeting note, a conversation, a review.
- **`live-model`**: maintained as understanding changes. Its current text is meant to be current; `amended` records each change.
- **`proposal`**: an idea put forward and not yet settled.
- **No `note_class`**: shown as "Unclassified". Read it as a record of its date until someone checks it.

The field is `note_class`, not `class`, and its values have two words each, because "class", "model" and "record" carry other meanings in many repositories.

`status` and `type` are labels only. Put longer prose about a note's standing in `summary`, not `status`.

### Labels

| Field | Type | Becomes |
|---|---|---|
| `tags` | list of strings with no `:` | topics (`tags`, kind `ad-hoc`), named as written |
| `labels` | map of a facet name to a list of strings | topics named `<facet>:<value>`, with the facet as their kind, such as `chapter:Ch3` |

```yaml
labels:
  chapter: [Ch3]
  project: [alpha, beta]
```

- A facet becomes a filter in the Library, such as "Chapter: Ch3".
- The note's folder is always a label too, named `folder:<path>`. It needs no field.
- Every label LeStash makes is named `<facet>:<value>`, with the facet as its kind: `labels`, `note_class`, `status`, `type`, `medium`, folders and vocabulary mentions.
- A topic name is unique across kinds. The prefix keeps labels apart from each other, and a tag can't hold `:`, so tags never collide with labels.
- So these facet names are reserved, and the schema rejects them under `labels`: `ad-hoc`, `category`, `folder`, `mention`, `note_class`, `status`, `type` and `medium`.

### Links

Each field holds one reference or a list of them.
A reference is one of these:

- a path to another note, relative to the note itself, as in a markdown link: `../design/overview.md`;
- a path from the repository's root, starting with `/`, as GitHub reads it: `/docs/design/overview.md`;
- a web address: `https://arxiv.org/abs/2401.00001`;
- a LeStash item: `lestash:12345`.

| Field | Becomes a link of kind | Meaning | Allowed target |
|---|---|---|---|
| `related` | `references` | this note points to it | any item or web address |
| `derived_from` | `derived_from` | this note was built from it, such as a summary of a talk or a chat | any held item |
| `alternate_of` | `alternate_of` | the same note kept elsewhere, such as the voice recording it transcribes (LeStash's voice recordings are notes) | another `note` |
| `revision_of` | `revision_of` | this note is the next version of it | an earlier `note` |

- The allowed ends follow the ontology's link table ([content-and-activity-ontology-design.md](content-and-activity-ontology-design.md#typed-links)).
- An `alternate_of` whose target is not a note is stored as `derived_from`, and the sync report lists it.
- Links in the note's body become `references` too. They need no field.
- `alternate_of` to a LeStash item doesn't make a duplicate. Search shows the two as one result.
- A reference that resolves to nothing is kept as a plain link, and listed in the source's sync report.

### Captures of external material

A capture is a copy of something someone else wrote, or of another repository's file.
It is imported with `own: false` and kept apart from your notes.

```yaml
capture:
  type: verbatim          # verbatim | reference | generated
  url: https://example.org/post
  repo: someone/project   # when the original is a repository file
  ref: "0123456"          # its commit or tag; quoted, or YAML reads a number
  path: docs/post.md
  retrieved: "2026-01-15"
  sha1: 0123456789abcdef0123456789abcdef01234567
  upstream_status: no durable upstream; this capture is the record
```

| `capture.type` | Meaning | Shown as |
|---|---|---|
| `verbatim` | an exact copy, never edited | "Capture · verbatim" |
| `reference` | a copy kept for reference, possibly trimmed | "Capture · reference" |
| `generated` | made by a model, not a primary source | "Capture · generated" |

- `sha1` records the original's checksum, as the capture's source gave it, in either case. LeStash stores it and never recomputes it.
- A `verbatim` capture should never change. When a later commit changes its body, the sync still updates the item, and lists the note in its report.

### Leaving a note out

| Field | Effect |
|---|---|
| `lestash: skip` | the note is not imported, and an existing item from it is left alone |

## What is not frontmatter

These belong to the source's settings in LeStash's config, not to any note:

- the repository, folder, branch and display name;
- files to leave out, by pattern;
- a vocabulary file whose headings are matched in notes as "mentions" (see below);
- the repository that `#123` issue references point to.

A vocabulary match is a label named `mention:<term>`, of kind `mention`. It says the note uses the term, never that it defines it.
Only headings of two or more words are matched, because single words mislabel too often.

## Versions and history

- Each note is keyed by its path. Git rename detection keeps the key when a note moves.
- Each commit that changes a note is a version in the item's history.
- A note deleted from the repository is marked gone, not deleted from LeStash.

## A full example

```yaml
---
title: Retry budgets for the build agent
date: "2026-09"
summary: Why each tool call gets a retry budget, and what spends it.
note_class: proposal
status: draft
type: position
medium: typed
tags: [agents, reliability]
labels:
  chapter: [Ch5]
related:
  - ../design/retry_policy.md
  - https://arxiv.org/abs/2401.00001
alternate_of: lestash:12345
---
```

Say the note sits in the folder `agents/`.
LeStash shows it as a proposal dated Sep 2026, with the tags `agents` and `reliability`, and the labels `note_class:proposal`, `status:draft`, `type:position`, `medium:typed`, `chapter:Ch5` and `folder:agents`.
It links to one note, one paper and one LeStash item.

## Versioning this schema

- The sidecar names the schema version it was written for: `"schema": 1`.
- Version 1 leaves out reply and containment links (`in_reply_to`, `part_of`). A later version can add them.
- A new field is a minor change. LeStash keeps an unknown field rather than rejecting it.
- Changing a field's meaning needs a new version number.
