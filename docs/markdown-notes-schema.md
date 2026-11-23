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
- [`schemas/markdown-notes.schema.json`](schemas/markdown-notes.schema.json) states the same rules as JSON Schema, so a repository can check its notes.

## Where the frontmatter comes from

LeStash reads a note's fields from two places, in this order:

1. **The note's own YAML frontmatter**, between `---` lines at the top of the file.
2. **A sidecar file**, `.lestash/notes.json` at the folder's root, keyed by each note's path.
   - It holds fields for notes that should not be edited, such as historic or verbatim ones.
   - A sidecar field replaces the same field in the note.
   - A reformatter can generate it, so the notes themselves stay untouched.

```json
{
  "schema": 1,
  "notes": {
    "meetings/2026-03_kickoff.md": { "class": "record", "labels": { "project": ["alpha"] } }
  }
}
```

## The fields

### Identity and dates

| Field | Type | Becomes | Fallback when absent |
|---|---|---|---|
| `title` | string | the item's title (`dcterms:title`) | the first `#` heading, then the filename |
| `date` | `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, or an ISO 8601 date-time | when it was written (`dcterms:created`); the precision is kept | a date in the filename, then the note's first git commit |
| `summary` | string, one or two sentences | the preview on cards | the first paragraph |
| `kind` | an item kind: `note` (default), `document`, `transcript` | the item's kind | `note` |
| `medium` | `typed`, `voice`, `conversation`, `correspondence`, `mixed` | how the note was made | none |
| `own` | boolean | whether you wrote it (`is_own_content`) | true, or false under `capture` |
| `author` | string | the item's author, when `own` is false | the repository's owner |

- A filename date is either `YYYY-MM-DD` at the start, or a month and year at the end: `_nov2026`.
- A date given to the month is shown as a month, such as "Nov 2026". LeStash never invents a day.
- The git commit date is the weakest sign: a bulk move or a rename resets it.

### How to read it

| Field | Values | Shown as |
|---|---|---|
| `class` | `record`, `model`, `proposal` | a label, and a banner on the item's page |
| `status` | a short phrase, such as `draft`, `open` or `confirmed` | a label |
| `type` | the repository's own word for the note, such as `review` or `position` | a label, as written |
| `superseded_by` | a reference (see below) | a banner linking the newer note |

The three classes say how far to trust a note today:

- **`record`**: correct as of its date, and not maintained since. A meeting note, a conversation, a review.
- **`model`**: maintained as understanding changes. Its current text is meant to be current.
- **`proposal`**: an idea put forward and not yet settled.
- **No `class`**: shown as "Unclassified". Read it as a record of its date until someone checks it.

`status` and `type` are labels only. Put longer prose about a note's standing in `summary`, not `status`.

### Labels

| Field | Type | Becomes |
|---|---|---|
| `tags` | list of strings | topics (`tags`, kind `ad-hoc`) |
| `labels` | map of a facet name to a list of strings | topics with the facet as their kind, such as `chapter` or `project` |

```yaml
labels:
  chapter: [Ch3]
  project: [alpha, beta]
```

- A facet becomes a filter in the Library, such as "Chapter: Ch3".
- The note's folder is always a label too. It needs no field.

### Links

Each field holds one reference or a list of them.
A reference is one of these:

- a path to another note, relative to the folder's root or to the note: `../design/overview.md`;
- a web address: `https://arxiv.org/abs/2401.00001`;
- a LeStash item: `lestash:12345`.

| Field | Becomes a link of kind | Meaning |
|---|---|---|
| `related` | `references` | this note points to it |
| `derived_from` | `derived_from` | this note was built from it, such as a summary of a talk |
| `alternate_of` | `alternate_of` | the same thing kept elsewhere, such as the voice recording this note transcribes |
| `revision_of` | `revision_of` | this note is the next version of it |
| `reply_to` | `in_reply_to` | this note answers it |
| `part_of` | the item's parent | this note is an excerpt or a section of it |

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
  ref: 0123456            # its commit or tag
  path: docs/post.md
  retrieved: 2026-01-15
  sha1: 0123456789abcdef0123456789abcdef01234567
  upstream_status: no durable upstream; this capture is the record
```

| `capture.type` | Meaning | Shown as |
|---|---|---|
| `verbatim` | an exact copy, never edited | "Capture · verbatim" |
| `reference` | a copy kept for reference, possibly trimmed | "Capture · reference" |
| `generated` | made by a model, not a primary source | "Capture · generated" |

- When `sha1` is given and the body no longer matches it, the sync flags the note rather than updating it silently.

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

A vocabulary match is a label of kind `mention`. It says the note uses the term, never that it defines it.
Only headings of two or more words are matched, because single words mislabel too often.

## Versions and history

- Each note is keyed by its path. Git rename detection keeps the key when a note moves.
- Each commit that changes a note is a version in the item's history.
- A note deleted from the repository is marked gone, not deleted from LeStash.

## A full example

```yaml
---
title: Retry budgets for the build agent
date: 2026-09
summary: Why each tool call gets a retry budget, and what spends it.
class: proposal
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

LeStash shows it as a proposal dated Sep 2026, labelled `draft`, `position`, `agents`, `reliability` and `Ch5`.
It links to one note, one paper and one LeStash item.

## Versioning this schema

- The sidecar names the schema version it was written for: `"schema": 1`.
- A new field is a minor change. LeStash keeps an unknown field rather than rejecting it.
- Changing a field's meaning needs a new version number.
