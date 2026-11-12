# UX Object Model — an ORCA pass over the ontology

Status: decisions taken 11 Nov 2026 (see "Decided"). Tracked in #268 (waypoints U1–U7). Refs #257, #222.

## Why

The web app is a feed of identical cards, sorted by source.
The ontology (`content-and-activity-ontology-design.md`) now says what things are, who made them, what you did, and how they link.
The app shows little of that, so the walk-through of 11 Nov 2026 found pages that stack, lists that hide what each row is, and no way to reach a person.

This document designs the screens from the ontology, not from the sources.

## Method

- **ORCA**, from Object-Oriented UX (Sophia Prater): Objects, Relationships, Calls to action, Attributes.
- This is round one, *discovery*: name the objects, map how they relate, list what you do with them and what they hold.
- Each object gets one **card** (how it looks in a list) and one **page** (its own address).
- Cards are kept, but each object and kind gets its own card face. One card for everything is the problem, not cards.
- Supporting sources: faceted navigation (Marti Hearst, *Search User Interfaces*); entity pages and backlinks (Wikipedia, IMDb, Obsidian); Nielsen's heuristics for the findings.
- Counts below are from the live database on 11 Nov 2026, read-only.

## O — Objects

| Object | What it is to you | Ontology | Count |
|---|---|---|---|
| **Item** | Anything you keep: a post, a book, a paper, a video, a chat, a notebook, a note | `items` | 104,864 (26,104 of them parts of another) |
| **Person** | Someone or something that makes or receives items: a person, a channel, a group, an AI | `people` | 18,781 (590 people, 18,187 organisations, 1 group, 3 software) |
| **Activity** | Something you did at a time: watched, liked, prompted, searched | `activities` | 85,754 over 2,196 days |
| **File** | A recording, PDF, slide image or book file that an item holds | `item_media`, `blobs` | 4,703 |
| **Topic** | A subject you tag items with | `tags` | 23 |
| **Collection** | A set of items you put together, with a description | `collections` | 13 |

Not objects, and why:

- **Link** is a relationship between two items, so it lives in R.
- **Account** (`person_identities`) belongs inside Person; you never browse accounts alone.
- **Source** is configuration: connect, sync, see errors. It moves to Settings.
- **Day** is a view of Activities, not a thing of its own. It still gets an address, `/day/<date>`.

### Item kinds

An item's kind decides its card face and the extra section on its page.
No `kind` field exists today; it is derived from source and metadata.

| Kind | Where it comes from | Extra on its page |
|---|---|---|
| Post | LinkedIn, Bluesky, Micro.blog | Thread of replies; cross-posts |
| Comment / reply | LinkedIn, YouTube | The post it answers, in place |
| Reaction | LinkedIn likes | Never a page of its own; grouped on its post |
| Message, invitation | LinkedIn | Conversation |
| Video | YouTube (71,673) | Player; transcript; how often you watched it |
| Book | Kobo, Audible, Jelu | Files; highlights, clips, notes, bookmarks as parts |
| Paper | arXiv | Authors; papers that cite or are cited |
| Chat | Claude, Gemini, Mistral, Claude Code | Turns, with who spoke |
| Notebook | NotebookLM (55) | Sources (`derived_from`); generated artifacts by type |
| Generated artifact | NotebookLM: slides 98, infographic 72, audio overview 32, explainer video 21, app 13 | Player or slides beside the text; link up to its notebook |
| Document | Google Drive (351), web pages, PDFs | File viewer |
| Note | Voice notes, typed notes | Recording; what it annotates |
| Email thread | Gmail | Messages in order |
| Stub | Something named but not held (1,149) | "Not in LeStash" and where it lives |

## R — Relationships

Read each row as "an [row] … [column]".

| | Item | Person | Activity | File | Topic | Collection |
|---|---|---|---|---|---|---|
| **Item** | has parts / part of (26,104); replies to (895); references (883); cross-post of (76); built from; same as; similar to (W4) | made by: author 27,667, creator 62,207, narrator 43, publisher 38; sent to 5; mentions 457 | was done to it (85,754) | has files (4,703) | is about | is in |
| **Person** | made, voiced, published, received, is named in | same person as (accounts) | did (you, and others' likes) | — | — | — |
| **Activity** | was done to one item; in the context of another (a Gemini prompt using a notebook) | done by | on the same day as | — | — | — |
| **File** | belongs to an item | — | — | versions of | — | — |
| **Topic** | tags items | — | — | — | — | — |
| **Collection** | holds items, in order, with notes | — | — | — | — | — |

What this means for screens:

- Every cell with a count needs a place on both objects' pages, labelled by its relationship.
- Inverse names follow the ontology: "referenced by", "replies", "source of", "original".
- Item ↔ Item is the richest cell, but today it shows only as "N children" and "Similar items".

## C — Calls to action

✓ exists today, ✗ missing.

| Object | Actions |
|---|---|
| Item | ✓ open; ✓ open original; ✓ tag; ✓ add to collection; ✓ add note; ✓ link to item; ✓ edit; ✓ history; ✓ copy link; ✓ share to Micro.blog; ✗ play from a transcript time; ✗ go up to its parent; ✗ see it in its day |
| Person | ✗ open; ✗ see their items by role; ✗ see your activity with them; ✗ name an unnamed account; ✗ merge two that are the same; ✓ open profile (only for you) |
| Activity | ✗ open the day; ✗ filter by verb; ✓ (indirect) open the item |
| File | ✓ play or open; ✗ download; ✗ jump to a slide; ✗ see which items share it |
| Topic | ✓ filter the feed; ✗ open a topic page; ✗ rename or merge |
| Collection | ✓ create, edit, add, remove; ✓ open; reorder not checked |

## A — Attributes

| Object | Core content | Metadata | Sort and filter by |
|---|---|---|---|
| Item | title, text, transcript | kind, source, date, held or stub, own or others' | kind, person, date, topic, collection, source, own |
| Person | name | kind, accounts, is you | kind, number of items, last seen |
| Activity | verb, item | time, source, context item | verb, date, item kind |
| File | the file | type, size, version, extracted-text tool | type |
| Topic | name | number of items | name, size |
| Collection | name, description | number of items, updated | updated, name |

## Navigation

Top level, by object, not by source:

| Place | Address | What it shows |
|---|---|---|
| Search | `/search?q=…&kind=…` | Results as kind cards, with facets: kind, person, date, topic, source |
| Timeline | `/day/<date>`, `/` | Your activities and new items, newest day first |
| Library | `/library?kind=book` | Items by kind: Books, Papers, Videos, Notebooks, Posts, Chats, Notes |
| People | `/people`, `/people/<id>` | People by kind and by how much they appear |
| Collections | `/collections/<id>` | As now |
| Item | `/items/<id>` | The item page below |
| Settings | `/settings/…` | Sources, Stats, Debug, connections |

- One router reads the address and shows exactly one page. This is what fixes #257.
- Every page has the top bar; Back is the browser's, plus "up" to the parent where there is one.
- On a phone: a bottom bar with Search, Timeline, Library, People; Collections and Settings under a menu.

## Wireframes

Illustrative: the layout is the proposal; titles, dates and counts are examples.

### Item page — a notebook

```
┌───────────────────────────────────────────────────────────────┐
│ LeStash   Search  Timeline  Library  People  Collections   ⚙ │
├───────────────────────────────────────────────────────────────┤
│ Library › Notebooks                                           │
│ ▣ Reliable Code Generation via Dual-State Neuro-Symbolic Arch.│
│   Notebook · NotebookLM · 21 Jan 2026 · by You                │
│   [Tag] [Collection] [Note] [Link] [Open in NotebookLM] [⋯]   │
├──────────────────────────────────────┬────────────────────────┤
│ Generated (10)                       │ Built from (9)         │
│ ▶ Audio overview   Fixes AI Code…  4m│  ▤ README.md  (web)    │
│ ▶ Audio overview   Bounding AI…    6m│  ▤ Masters report (pdf)│
│ ▶ Explainer video  A Perfect System  │  ▤ 3 images            │
│ ▦ Slides           …          12 pp  │  ◌ 2 not in LeStash    │
│ ▦ Infographic      Fiabilité… (fr)   │                        │
│ ◇ App              Flashcards        │ Your activity          │
│                                      │  prompted Gemini with  │
│ Chats (1) · Notes (0)                │  it · 3 Feb 2026       │
│                                      │                        │
│ Referenced by (4)                    │ Topics  atomicguard    │
│  ▭ Post  "Own your decisions…"       │ In  Formal Agents      │
└──────────────────────────────────────┴────────────────────────┘
```

### Item page — an audio overview

```
│ Library › Notebooks › Reliable Code Generation…   (up link)    │
│ ▶ AtomicGuard Fixes AI Code Hallucinations                     │
│   Audio overview · 4 min · English · Short · two hosts         │
│ ┌──────────────────────────────────────────────────────────┐   │
│ │ ▶ ━━━━━━━━━○──────────────  1:02 / 4:21   (stays on top)│   │
│ └──────────────────────────────────────────────────────────┘   │
│ Prompt: "Make a down to earth summary of how the framework…"  │
│                                                                │
│ Speaker 1  0:00  You know that feeling? …       (time = seek)  │
│ Speaker 2  0:03  code and it spits out …                       │
```

- Settings shown in words ("Short", "two hosts"), not `EPISODE_LENGTH_SHORT`.
- The title appears once.

### Item page — a post with its thread

```
│ ▭ Own your decisions. I don't mean accountability…            │
│   Post · LinkedIn · 20 Jan 2026 · by You                      │
│   ♥ 14 people reacted  ▸ (opens the list, each a person link) │
│                                                                │
│ Thread (9), oldest first                                       │
│  You           20 Jan  I'd like to invoke the wisdom of …      │
│    ↳ A contact 20 Jan  …you may be waiting some time …         │
│    ↳ You       21 Jan  that's a fire hose of insight …         │
│                                                                │
│ Mentions  4 people · Cross-posted to  Bluesky                  │
```

- Replies don't repeat the post's first line.
- Reactions are one line, not 14 cards; unnamed accounts read "a LinkedIn member", with a "name them" action.

### Person page

```
│ ● <Name>                                    Person · 3 accounts│
│   LinkedIn · YouTube · arXiv            [Merge…] [Rename]      │
├───────────────────────────────────────────────────────────────┤
│ Made        Papers 3 · Videos 12 · Posts 40                    │
│ With you    replied to your posts 9 · reacted 14 · mentioned 2 │
│ Your activity  watched 12 of their videos · last 4 Nov 2026    │
│                                                                │
│ [cards of their items, filtered by the chips above]            │
```

- The same page serves a channel (organisation) and an AI (software); sections hide when empty.
- Your own page is "You", reached from the top bar.

### Timeline — one day

```
│ ‹ Mon 10 Nov 2026 ›                         [verbs ▾] [kinds ▾]│
│ 09:12  watched   ▶ Video   Anthropic Replaced Git…             │
│ 09:40  prompted  ✦ Chat    Gemini · used notebook "Reliable…"  │
│ 11:05  new       ▭ Post    You on LinkedIn                      │
│ 14:30  liked     ▶ Video   …                                   │
│ … 74 watches folded: "watched 61 videos" ▸                     │
```

- Busy verbs fold into one line, so a day of watching doesn't bury the rest.

### Library — books

```
│ Library   Books (121) Papers Videos Notebooks Posts Chats Notes│
│ [person ▾] [topic ▾] [date ▾] [held ▾]          sort: recent ▾ │
│ ▤ Build a Large Language Model (From Scratch)                  │
│   Book · epub · by <author> · 11 Oct 2024 · 0 highlights       │
│ ▤ Neural Networks for Beginners                                │
│   Book · 75 highlights                                         │
```

- Filters are applied by the server before paging, so each page is full (the 3-of-20 bug).

## Card faces

Every card has the same frame: type icon, title, one line of who and when, one line of what's inside.

| Kind | Second line | Third line |
|---|---|---|
| Post | source · date · by | 9 replies · 14 reactions |
| Comment | "on" parent title · date · by | first line of the comment |
| Video | channel · date | watched 3 times · transcript |
| Book | author · format | 75 highlights · files |
| Notebook | date | 9 sources · 10 generated |
| Generated artifact | its notebook · type | length or pages |
| Chat | AI · date | 12 turns |
| Person | kind · accounts | 40 items · last seen |
| Stub | "not in LeStash" · where | what links to it |

Cards are links (`<a>`), so the keyboard reaches them.

## Walk-through findings this design covers

| Finding (11 Nov 2026) | Covered by |
|---|---|
| Pages stack; address and page disagree (#257) | One router, one page per address |
| No way out of an item but Back | Top bar on every page; "up" to parent |
| Filtered feed pages nearly empty | Server-side filters before paging |
| Children don't say what they are | Kind card faces |
| No link from an artifact up to its notebook | Breadcrumb and "up" |
| Player below the transcript; times don't seek | Sticky player; times are seek links |
| Raw enums | Settings shown in words |
| Comments read backwards, repeat the post | Thread view, oldest first |
| Reactions as cards; URNs as names | Grouped reactions; unnamed accounts worded |
| No person page | Person page |
| Sources tab is settings | Sources in Settings; Library by kind |
| Half a phone screen is chrome | Bottom bar, filters folded |
| Cards not reachable by keyboard | Cards as links |
| Searches not in the address | `/search?q=…` |

Left for separate work: search ranking of near-empty sessions; speaker splits in fast exchanges; contrast just under 4.5:1.

## Gaps behind the screens

- **Item kind:** a `kind` derived once in `core/enrichment.py`, shared by CLI and server.
- **API:** `GET /api/people`, `GET /api/people/{id}` (items by role, activity), `GET /api/activities?date=…` (timeline), facet counts for search and library.
- **Feed filters** applied in SQL before `LIMIT`.
- **Reactions** returned as counts with their parent, not as items in lists.
- **Unnamed accounts** need a display rule and a "name them" action.

## Decided (11 Nov 2026)

### Library shelves
- Books, Papers, Videos, Notebooks, Posts, Chats, Notes.
- Shelves sort by what a thing *is*. Each item sits on one shelf: an exact scheme, in Rosenfeld and Morville's terms.

### Themes are not a shelf
- A theme cuts across shelves: a paper, a video and a notebook can share one. That is an ambiguous, topic scheme, so it gets its own place beside the shelves.
- **Collections and themes** is that place:
  - your collections, made by hand, as now;
  - *suggested themes*, inferred from the item vectors, shown as suggestions you can keep as a collection or dismiss.
- Suggested themes build on W4 (#219, related items as `suggested` links), so they come after it.

### Channels
- A channel is its own object on screen: **Channel**, beside Person and AI.
- Underneath it stays a `people` row of kind `organization`; no new table.
- They share one page, **People**, with a switch: People · Channels · AI. Each has its own card and page sections, such as "videos" and "you watched" for a channel.

### A channel is anything that publishes a series
- Not only YouTube: a blog, a podcast, a newsletter or a mailing list is a channel too.
- A channel is two things at once:
  - an agent that publishes (`schema:Organization`, ActivityStreams `Service` or `Group`);
  - a series of works (`schema:CreativeWorkSeries`, with `Blog` and `PodcastSeries` under it).
- So a channel page shows who it is, and its works newest first.
- **Following** is a relationship from you to a channel, with a date: YouTube subscriptions today; blog, podcast or newsletter feeds later.
- Gaps:
  - subscriptions aren't stored as relationships yet; the YouTube plugin names them, but no `subscribed` activity or link exists;
  - blogs need a feed source (RSS or Atom) when you start following any.

### Home is the Timeline
- `/` opens today on the Timeline, with the search box at its top.

### The Feed goes
- Its job, "what's new", becomes the Timeline's "added" entries, so a new item shows on the day it arrived.
- Industry practice for activity streams, followed here:
  - **Aggregate** repeated activity by verb, object kind and time: "watched 61 videos" as one line that opens. Stream's aggregated feeds group by such a key, with a count and a few examples.
  - **Group by day**, with day and month steps. Lifelog studies find a yearly scale useless for minute-level data (Teraoka) and that people cope with about 10 landmarks at a glance (Chen and Jones).
  - **Pair the timeline with search** (LifeLens 2.0), so you can jump or narrow.
  - **Landmarks:** your own posts, notes and new notebooks stand out; watches and likes fold.
- Feed filters (source, own, subtypes) become Timeline filters: verb and kind.

### Order of work
Decided by engineering, since each step needs the one before:

1. Router and top bar; fixes #257. Server-side filters before paging.
2. Item kinds and card faces; the item page (related, who, when); threads; grouped reactions.
3. Timeline as home; the activities API; aggregation by day.
4. People · Channels · AI; their pages and API.
5. Library shelves with facets.
6. Phone layout: bottom bar.
7. Suggested themes, after W4 (#219).

Each step ships on its own and is walked through before the next.

## Look and feel (11 Nov 2026)

The layout held up in the walk-through; the look did not. These are the choices, each a setting where taste differs.

- **Themes:** chosen in Settings, kept on the device. Nord is the default. Also Automatic (Nord, light or dark as the system is), Catppuccin, Rosé Pine, Gruvbox, Tokyo Night, Solarized, and LeStash's earlier colours. Every theme sets the same tokens (`css/themes.css`), and `scripts/adhoc/theme-contrast.mjs` checks them: 4.5:1 for text, 3:1 for icons.
- **Density:** compact by default; comfortable is the option.
- **Type:** sans-serif by default; serif for reading, or monospace throughout.
- **Rows, not boxes:** a list row has a tile on its left: the cover or thumbnail where there is one, else the kind's icon on its colour. Each kind takes one of the theme's eight colours.
- **The item opens over its list:** a sheet sliding up on a phone, a panel on the right on a wider screen. The list keeps its place; Back, Escape, the backdrop or a swipe down closes it. An item opened from a link is a whole page.
- **Show what differs, fold what repeats:** a chat's prompts fold under one Timeline line that opens to what you asked; replies show each author's initial, their name once per run, and mark whom they answer; a part's row in its parent's page leaves the parent out.
- **You are named, not "You":** bylines say "by Matt", from your person's short name, which you set on your page. Sentences addressed to you keep "you" ("You watched 466 of their videos (502 times), since Jan 2022").

## References
- Sophia Prater, Object-Oriented UX and the ORCA process.
- Louis Rosenfeld and Peter Morville, *Information Architecture*: exact and ambiguous organisation schemes.
- Marti Hearst, *Search User Interfaces*: faceted navigation.
- Nielsen Norman Group, *Streams, Walls, and Feeds* (2010) and the 10 usability heuristics.
- Stream, aggregated feeds: https://getstream.io/blog/scalable-activity-feed-architecture/
- Chen and Jones, timeline landmarks for lifelogs: https://doras.dcu.ie/15978
- Teraoka, calendar and timeline views of personal logs: https://link.springer.com/doi/10.1186/2192-1962-2-1
- LifeLens 2.0, search with a timeline: https://doras.dcu.ie/30674/1/3643489.3661119.pdf

## Phone feedback (12 Nov 2026, #277)

- **Header:** the microphone opens Record a note. Debug mode is a switch on the Debug page.
- **Help** (`/help`, under More): each source, how to ask for its export, and how LeStash takes it in.
- **Where an item comes from:** the item page names the service; a file from a repository shows "GitHub · owner/repo · file", linked.
- **Timeline:** rows and folded groups name the service ("23 posts on YouTube"); busy verbs fold per service.
- **Front matter:** a Markdown file's header block folds under "File details" instead of opening the text.
