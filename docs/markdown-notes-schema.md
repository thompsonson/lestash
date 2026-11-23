# Markdown Notes: Frontmatter Schema

*Proposed 2026-11-23, version 1. For the markdown notes source planned in #408.*

## Overview

LeStash can import a folder of markdown notes kept in a git repository.
This document says what it reads from each note, and how each field becomes part of an item.
A note is a file whose name ends in `.md`, in lower case; other files are only attachments.
A file ending in `.markdown`, or in `.md` in another case, is not imported, and the sync report lists it.

- The schema is the contract between a notes repository and LeStash.
- A repository can adopt it directly, or a reformatter outside LeStash can map its own conventions onto it.
- Every field is optional. A note with no frontmatter still imports, using the fallbacks below.
- Fields LeStash doesn't know are kept in the item's metadata, uninterpreted, and the sync report lists them.
  A common name from other tools is listed with the field it likely means: `date` → `created`, `summary` → `abstract`, `author` → `creator`, `tags` → `subject`, `amended` → `change_note`, `scope` → `scope_note`.
- Field names follow LeStash's naming rule: each takes the name of its published term, from the crosswalk in [content-and-activity-ontology-design.md](content-and-activity-ontology-design.md#canonical-sources-23-nov-2026-410).
  Each table below gives the published term and how closely it matches. A field with no published term says "none".
- Two JSON Schemas state the same rules, so a repository can check its notes:
  - [`schemas/markdown-notes.schema.json`](schemas/markdown-notes.schema.json) for one note's fields;
  - [`schemas/markdown-notes-sidecar.schema.json`](schemas/markdown-notes-sidecar.schema.json) for the sidecar's structure; each entry is checked with the note schema after merging.

## Where the frontmatter comes from

LeStash reads a note's fields from two places, in this order:

1. **The note's own YAML frontmatter**, between `---` lines at the top of the file.
2. **A sidecar file**, `.lestash/notes.json` at the folder's root, keyed by each note's path.
   - It holds fields for notes that should not be edited, such as historic or verbatim ones.
   - A sidecar entry replaces the note's own value field by field, at the top level only.
     A list or map in the sidecar, such as `subject` or `labels`, replaces the note's whole value; nothing is merged.
   - A reformatter can generate it, so the notes themselves stay untouched.
   - An entry with `read_frontmatter: false` stands in for the note's frontmatter, which is then not read at all.

```json
{
  "schema": 1,
  "notes": {
    "meetings/2026-03_kickoff.md": { "note_class": "record-of-exchange", "labels": { "project": ["alpha"] } }
  }
}
```

### When frontmatter doesn't parse, or breaks the schema

The frontmatter block is never part of the body, so an unparsed block never becomes the abstract.

LeStash first merges the sidecar entry over the note's frontmatter, then checks the result against the schema.
So a sidecar value replaces a broken frontmatter value before any check.

**The frontmatter can't be read**: it isn't valid YAML, or it isn't a mapping. A common cause is an unquoted value holding `": "`.

- If the note's sidecar entry has `read_frontmatter: false`, the note imports with the sidecar's fields alone.
- Otherwise the note is held back: not imported, and an existing item from it is left as it was.
  Its unread fields may say `lestash: skip`, or mark it a capture of someone else's work, so importing it could be wrong.

**After the merge, a field breaks the schema:**

- `lestash`, `own`, `creator` or `capture`: the note is held back. These fields decide whether, and as whose work, it is imported.
- Any other field, such as `note_class: model`: that field is ignored, and the note's other fields still apply.

**An `author` field with no `creator`, on a note that is its owner's**: the note is held back.
It would otherwise be credited to the repository's owner. A note with `own: false`, or a capture that doesn't set `own: true`, has no owner fallback, so it imports.
The JSON Schema states this rule too.

**The sidecar itself is broken**: it isn't valid JSON, its `schema` isn't 1, `notes` isn't an object, an entry isn't an object, or a key isn't a normalised path.

- The source's sync stops before changing anything, because the sidecar may hold skips and captures.

**A sidecar key names no file at the synced commit**, for example after a note moved or was deleted:

- The source's sync stops before changing anything, and reports the key.
- LeStash doesn't guess where a note went: git's rename detection is a guess, and a wrong guess would move a note's protecting fields to another note.
- A key for a file the source's settings leave out still names a file, so it is fine.

LeStash trusts the sidecar as it stands at the synced commit. Keeping it current is the notes repository's job.
Regenerate it in every commit that changes a note, for example with a pre-commit hook that fails when it is out of date.
How the source detects an entry left on a reused path is still open in #408.

Every held-back note, ignored field, unknown field and stopped sync is listed, with the reason, in the source's sync report.

## The fields

### Identity and dates

| Field | Type | Becomes | Fallback when absent | Published term |
|---|---|---|---|---|
| `title` | string | the item's title | the first `#` heading, then the filename | `dcterms:title`, exact |
| `created` | `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, or an ISO 8601 date-time | when it was written; the precision is kept | a date in the filename, then the note's first git commit | `dcterms:created`, exact |
| `abstract` | string, one or two sentences | the preview on cards | the first paragraph | `dcterms:abstract`, exact |
| `medium` | `typed`, `voice`, `conversation`, `correspondence`, `mixed` | how the note was made, as the label `medium:<value>` | none | `dcterms:medium`, close: Dublin Core means the carrier, this means how it was made |
| `own` | boolean | whether you wrote it (`is_own_content`) | true, or false under `capture` | none |
| `creator` | string | the item's author | the repository's owner when `own` is true; none when `own` is false | `dcterms:creator`, exact |

- Every item from this source has the kind `note`; the ontology derives kind from the source.
- A filename date is either `YYYY-MM-DD` at the start, or a month and year at the end: `_nov2026`.
- Quote every date, wherever it appears: `created: "2026"`, `"2026-11"`, `"2026-11-09"`.
  Unquoted, YAML reads a year as a number and a full date as a date object, and the JSON Schema rejects both.
  Before its own check, LeStash turns a YAML date, or a year read as a number, back into text, so it accepts either form.
  Quote any other value YAML might read as a number or a boolean, such as `commit: "0123456"` or `status: "on"`.
  YAML 1.1 reads `yes`, `no`, `on` and `off` as booleans. LeStash doesn't convert these.
- A date given to the month is shown as a month, such as "Nov 2026". LeStash never invents a day.
- The git commit date is the weakest sign: a bulk move or a rename resets it.

### How to read it

| Field | Values | Shown as | Published term |
|---|---|---|---|
| `note_class` | `record-of-exchange`, `live-model`, `proposal` | the label `note_class:<value>`, and a banner on the item's page | none; its values form a SKOS concept scheme |
| `status` | a short phrase, such as `draft`, `open` or `confirmed` | the label `status:<value>` | `schema:creativeWorkStatus`, exact |
| `type` | the repository's own word for the note, such as `review` or `position` | the label `type:<value>` | `dcterms:type`, close: Dublin Core prefers its own type vocabulary |
| `scope_note` | a phrase: what the note covers | a line under the title | `skos:scopeNote`, close: SKOS defines it for concepts |
| `change_note` | a list; each entry is `{date, summary}`, or a string `"YYYY-MM-DD — summary"`, where the date may be `YYYY`, `YYYY-MM` or `YYYY-MM-DD`, and the dash `—`, `–` or `-`, with a space each side | "Changed N times, last on …" on the item's page | `skos:changeNote`, close: SKOS defines it for concepts |

The three classes say how far to trust a note today:

- **`record-of-exchange`**: correct as of its date, and not maintained since. A meeting note, a conversation, a review.
- **`live-model`**: maintained as understanding changes. Its current text is meant to be current; `change_note` records each change.
- **`proposal`**: an idea put forward and not yet settled.
- **No `note_class`**: shown as "Unclassified". Read it as a record of its date until someone checks it.

The field is `note_class`, not `class`, and its values have two words each, because "class", "model" and "record" carry other meanings in many repositories.

`status` and `type` are labels only. Put longer prose about a note's standing in `abstract` or `scope_note`, not `status`.

### Labels

| Field | Type | Becomes | Published term |
|---|---|---|---|
| `subject` | list of strings | topics (`tags`, kind `ad-hoc`) | `dcterms:subject`, exact; each value a `skos:Concept` |
| `labels` | map of a facet name to a list of strings | topics named `<facet>:<value>`, with the facet as their kind, such as `chapter:ch3` | none for the map; each facet a `skos:ConceptScheme`, each value a `skos:Concept` in it |

```yaml
labels:
  chapter: [Ch3]
  project: [alpha, beta]
```

- A facet becomes a filter in the Library, such as "chapter: ch3".
- Topic names are stored in lower case, so `Ch3` and `ch3` are one label.
  Folders whose names differ only in case share one folder label too.
- The note's folder is always a label too, named `folder:<path>`. It needs no field.
- Every label LeStash makes is named `<facet>:<value>`, with the facet as its kind: `labels`, `note_class`, `status`, `type`, `medium`, folders and vocabulary mentions.
- A topic name is unique across kinds, and any source or the app can make a topic of any name.
  If a label's name already exists as a topic of another kind, the note gets that existing topic, and the sync report lists the clash.
  A facet filter matches topics by their `<facet>:` name prefix, not by kind, so a clash never hides a note from it.
  A subject spelled like a label, such as `status:draft`, is the same topic as that label.
- So these facet names are reserved, and the schema rejects them under `labels`: `ad-hoc`, `category`, `folder`, `mention`, `note_class`, `status`, `type` and `medium`.

### Links

Each field holds one reference or a list of them.
A reference is one of these:

- a path to another note, relative to the note itself, as in a markdown link: `../design/overview.md`;
- a path from the repository's root, starting with `/`, as GitHub reads it: `/docs/design/overview.md`;
- a web address: `https://arxiv.org/abs/2401.00001`;
- a LeStash item: `lestash:12345`.

| Field | Becomes a link of kind | Meaning | Allowed target | Published term |
|---|---|---|---|---|
| `references` | `references` | this note points to it | any item or web address | `dcterms:references`, exact |
| `derived_from` | `derived_from` | this note was built from it, such as a summary of a talk or a chat | any held item | `prov:wasDerivedFrom`, exact |
| `alternate_of` | `alternate_of` | the same note kept elsewhere, such as the voice recording it transcribes (LeStash's voice recordings are notes) | another `note` | `prov:alternateOf`, exact |
| `replaces` | `revision_of` | this note supersedes it; the older note shows "Replaced by" this one | an earlier `note` | `dcterms:replaces`, exact; the stored link is `prov:wasRevisionOf` |

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
  relation: quoted        # quoted | derived
  generated_by: Gemini    # only when software made the original
  source: https://example.org/post
  repo: someone/project   # when the original is a repository file
  commit: "0123456"       # quoted, or YAML reads a number
  path: docs/post.md
  retrieved: "2026-01-15"
  sha1: 0123456789abcdef0123456789abcdef01234567
  upstream_status: no durable upstream; this capture is the record
```

| Field | Meaning | Published term |
|---|---|---|
| `relation: quoted` | an exact copy, never edited; shown as "Capture · quoted" | `prov:wasQuotedFrom`, exact |
| `relation: derived` | a copy kept for reference, possibly trimmed; shown as "Capture · derived" | `prov:wasDerivedFrom`, exact |
| `generated_by` | the software that made the original, so it isn't a primary source; shown as "Generated by …" | `prov:wasGeneratedBy`, close: PROV names the activity, this names the software |
| `source` | where the original lives | `dcterms:source`, exact |
| `repo`, `commit`, `path` | the original's repository, commit and file | none |
| `retrieved` | when it was copied | none |
| `sha1` | the original's checksum | none |
| `upstream_status` | whether the original still exists | none |

- `sha1` records the original's checksum, as the capture's source gave it, in either case. LeStash stores it and never recomputes it.
- A `quoted` capture should never change. When a later commit changes its body, the sync still updates the item, and lists the note in its report.

### Leaving a note out

| Field | Effect | Published term |
|---|---|---|
| `lestash: skip` | the note is not imported, and an existing item from it is left alone | none |
| `read_frontmatter: false` | sidecar only: the entry stands in for the note's frontmatter | none |

## What is not frontmatter

These belong to the source's settings in LeStash's config, not to any note:

- the repository, folder, branch and display name;
- files to leave out, by pattern;
- a vocabulary file whose headings are matched in notes as "mentions" (see below);
- the repository that `#123` issue references point to.

A vocabulary match is a label named `mention:<term>`, of kind `mention`. It says the note uses the term, never that it defines it.
Only headings of two or more words are matched, because single words mislabel too often.

## Versions, moves and history

How the source follows a moved note, and which past versions it keeps, are not part of this schema.
They are open questions for the source itself, listed in #408.
This schema covers only the fields read at the synced commit.

## A full example

```yaml
---
title: Retry budgets for the build agent
created: "2026-09"
abstract: Why each tool call gets a retry budget, and what spends it.
note_class: proposal
status: draft
type: position
medium: typed
subject: [agents, reliability]
labels:
  chapter: [Ch5]
references:
  - ../design/retry_policy.md
  - https://arxiv.org/abs/2401.00001
alternate_of: lestash:12345
---
```

Say the note sits in the folder `agents/`.
LeStash shows it as a proposal dated Sep 2026, with the topics `agents` and `reliability`, and the labels `note_class:proposal`, `status:draft`, `type:position`, `medium:typed`, `chapter:ch5` and `folder:agents`.
It links to one note, one paper and one LeStash item.

## Versioning this schema

- The sidecar names the schema version it was written for: `"schema": 1`.
- Version 1 is the first published version. Field names in earlier drafts were never imported, so they need no version of their own.
- Version 1 leaves out reply and containment links (`in_reply_to`, `part_of`). A later version can add them.
- A new field is a minor change. LeStash keeps an unknown field rather than rejecting it.
- Changing a field's meaning needs a new version number.
