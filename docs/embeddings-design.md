# Embeddings: what to embed, and how to decide

Status: proposal for #221, written 12 Nov 2026, before W4 (#219, related items).
The evidence, with sources, is in [`embedding-research.md`](embedding-research.md).

## Why now

- W4 suggests related items from vectors, so the vectors must be right first.
- Semantic search found the expected item for 4 of the 20 baseline queries (#229), and 0 of the 10 paraphrase and "deep" ones.
- The decision should come from an experiment on LeStash's own data, not from a default.

## Where we are

- **Model:** `all-MiniLM-L6-v2`, 384 dimensions, 256 tokens (`core/embeddings.py`).
- **Text embedded:** title plus the first 500 characters, written out in three places.
- **Coverage:** 6,057 of 80,127 top-level items have a vector (7.6%).
  - Nothing has been embedded since August. Sync never embeds; only `lestash embeddings rebuild` does, and it only fills gaps.
  - Children are never embedded, so a chat is searched by its title: its 24,470 turns hold 45M characters.
- **Text past character 500:** 52% of top-level text, and 82% of children's.
- **Search:** FTS5 keyword plus vectors, fused with RRF (k = 60); near-empty items weighed down.

## What the evidence says

Measured results unless marked; details and links in the research doc.

- **The model matters more than the chunker.**
  - On real documents, fixed-size chunks did as well as or better than "semantic" chunking.
  - The best-measured gains come from a better embedder, hybrid keyword + vector search, and a reranker.
- **Search engines index whole documents but score passages.** Google's passage ranking keeps pages as the unit and adds a passage signal.
  - In research, scoring a document by its best passage (MaxP) beat its first passage on one test collection, and lost on another.
- **What others write about an item is a strong field.** Anchor text is the classic case.
  - LeStash's equivalents: comments, highlights, link text and NotebookLM summaries.
- **Chats are best searched by round,** one prompt plus its reply, for answering.
  - Whole sessions find more of the right sessions, though.
  - Adding extracted facts to the search key helped both.
- **Context helps a passage.** Prefixing each passage with where it sits cut failed retrievals in Anthropic's tests (vendor-measured).
  - The free version of this is the title and heading. The LLM version is slow on a CPU.
- **Better small models exist.**
  - `bge-small-en-v1.5` is a drop-in for MiniLM, about 10 retrieval points better and the same size of vector.
  - `granite-embedding-small-english-r2` reads 8k tokens.
  - EmbeddingGemma-300m and Qwen3-Embedding-0.6B are multilingual, which matters for the French content.
- **Related items work best from several signals,** each shown with its reason:
  - content similarity;
  - shared rare words ("more like this");
  - the graph: links, the same person or channel, the same collection, the same session.

## The ontology already carves things up

Fixed-size chunking cuts text where a counter says. LeStash's ontology already splits most content where its meaning splits, so the first rule is: **embed the ontology's own units.**

| Object (ontology) | Unit to embed | Context to prefix | Notes |
|---|---|---|---|
| Post, comment, message | The whole item | Author; for a comment, the post it is on | Short: one vector each |
| Chat | Each round: a prompt and its reply (the turns are its parts) | Chat title | Plus one vector for the chat, from its rounds |
| Note, paper, document, article | Passages within the item's own headings and pages (W3 gives both) | Title and heading | Plus one item vector; search scores by best passage |
| Book | Its highlights and notes (its parts) | Book title, chapter | The text of a book isn't held |
| Notebook (NotebookLM) | Its artifacts (parts), treated as documents | Notebook title | The notebook's summary is its own top level |
| Video | Title and description; transcript passages when held | Channel | 66k watch-history titles: see below |
| Search | The query | Engine | A link to what was read next, more than a target |
| Reaction | Nothing | | It's an activity on a post, not text of its own |
| Person, channel | The average of their items' vectors | | "Who writes about X" |
| Topic, collection | The average of their items' vectors | | Seeds suggested themes (U7) |
| Activity, link | Not embedded | | Signals for related items, and test pairs for W4 |

- **Only long items with no parts get split,** along their own structure; fixed size is the fallback inside a long section.
- **Every vector records its model, recipe and text hash,** so a change re-embeds only what changed.

## Experiments

Run on a copy of the database in a test environment, on a frozen test set, reporting results per kind of item.

1. **Test set.**
   - About 150 hand-written queries: known-item, paraphrase, deep, inside a chat, and French.
   - Add LLM-written queries from whole items, filtered by hand, for breadth only.
   - About 30 seed items with their related items judged. Your confirmed links are ready-made pairs.
2. **E1, baseline:** keyword alone; today's vectors alone; both fused.
3. **E2, coverage:** today's model and recipe, with every item and part embedded. This separates the coverage gap from the model gap.
4. **E3, model:** `bge-small-en-v1.5`, `granite-embedding-small-english-r2`, EmbeddingGemma (256 and 768 dimensions), Qwen3-Embedding-0.6B. Time each per 1,000 items on pop-mini.
5. **E4, units:** ontology units vs fixed 200- and 400-token passages vs first 512 tokens. Best passage vs mean vs first passage, for scoring.
6. **E5, chats:** round vs session vs both fused, with and without the chat title.
7. **E6, ranking:** RRF vs a tuned weighted sum; with and without a small reranker (`ms-marco-MiniLM-L6-v2`) on the top 30, timing each.
8. **E7, related items:** content alone vs adding keyword vs adding the graph, on the 30 seeds.

**How to decide:**
- Use paired comparisons per kind of item, not the overall average.
- Pick the smallest model within noise of the best, because re-embedding and search time recur.

## Decisions for Matthew

- **Watch-history videos:** 66k items that are just titles.
  - Proposal: embed them, but leave them out of semantic search and related items by default.
- **LLM enrichment** (contextual prefixes, generated questions): it needs a local LLM, which is slow on this CPU, or a cloud one, which breaks local-only.
  - Proposal: leave it out unless E5 shows the cheap prefixes fall short.
- **Freshness, whatever the results:**
  - embed after every sync;
  - re-embed when text changes;
  - one shared recipe instead of three.
  - Proposal: build this first, with today's model, as it's needed by every option.

## Supersedes

- `docs/content-and-activity-ontology-design.md`, "Decided": "No chunking for now… one vector per item (#221)". This doc replaces that decision once the experiments choose.
- `docs/vector-search-design.md`: title plus 500 characters, and embed-on-insert, which was never built.
