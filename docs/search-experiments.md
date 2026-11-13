# Search experiments, November 2026

Results of the search experiments planned in [`embeddings-design.md`](embeddings-design.md) ("Experiments", "Targets"), for #221. Run on 13 Nov 2026 on pop-mini against a copy of the live database (1.101.1). E3 is partial: three of the five models were still embedding when this was written.

## Summary

- **Hybrid search is the right default.** It beats keyword and semantic search alone on every kind of query but known items, where semantic alone is slightly ahead.
- **Three changes help, and they add up:**
  - embed chat rounds without the chat's title: +4 chat queries found (14 → 18 of 30);
  - a better small model, `bge-small-en-v1.5`: hybrid 83 → 88 of 150, MRR 0.40 → 0.44;
  - a reranker reading each candidate's start and best-matching window: MRR 0.40 → 0.48, known items 0.63 → 0.86.
- **Best combination so far:** untitled rounds plus the reranker: 89 of 150 in the top 10, MRR 0.49, against 82 and 0.40 today.
- **Two changes didn't pay:**
  - embedding whole texts in passages (E2) finds deep passages but crowds out short items: hybrid 82 → 83;
  - whole-chat vectors lose to rounds.
- **Still short of the targets** (75% overall): the best run finds 59%. Paraphrase queries are the weakest kind, at 13–15 of 30.

## How keyword, semantic and hybrid search work together

| | Keyword | Semantic | Hybrid |
|---|---|---|---|
| What it matches | the words typed | the meaning | both |
| How | SQLite FTS5 over title, content and author; BM25 ranking; every word a prefix; no stemming | each query and unit becomes a 384-number vector (`all-MiniLM-L6-v2`); nearest vectors win | both lists fused by Reciprocal Rank Fusion (k = 60) |
| What it sees | the whole text of every item | each item's title and first 500 characters; each chat round (prompt 400 and replies 600 characters); comments; highlights | both |
| Strong at | exact names, rare words, text deep in long items | paraphrase, loosely remembered titles, other wording | anything one of the two finds |
| Weak at | other wording (0 of 30 paraphrase queries) | text past the first 500 characters (1 of 30 deep queries) | it inherits both halves' misses |

- **Grouping:** both halves report parts (a round, a comment, a highlight) as their item, at the rank of the best part (#295).
- **Fusion:** each item scores 1/(60 + rank) from each list it appears in, so an item found by both rises. Near-empty items are scaled down by their length, so a bare title doesn't win on one word.
- **E1 shows the division of labour:** keyword alone found 47 of 150, semantic alone 66, hybrid 81. Hybrid recovered keyword's deep hits and semantic's paraphrase hits at once.

## Method

- **Test set:** 150 queries drafted from your own items, each naming the items it should find. It is a draft that you haven't yet reviewed.

  | Kind | Queries | What it tests |
  |---|---|---|
  | known-item | 35 | something you remember, roughly by its name |
  | paraphrase | 30 | the idea in other words |
  | deep | 30 | a passage over 500 characters into a long item |
  | chat | 30 | one round in the middle of a chat, off the chat's title |
  | French | 25 | French to French, French to English, and English to French |

- **Harness:**
  - The real server code runs on a copy of the database; only its vector lookup is swapped for the vectors under test.
  - Keyword search, grouping and fusion are therefore exactly as live.
  - The same API recorder and browser screenshots run against it.
  - Checked against live: 441 of 450 top-10 lists are identical.
- **Measures:**
  - Hits@10: the target is in the top 10.
  - MRR@10: mean reciprocal rank, the average of 1/rank of the target, counting 0 if it's outside the top 10.
- **For chat queries:** a hit means either turn of the target round.
- **Comparisons:**
  - Runs are compared query by query, with a two-sided sign test.
  - With 30 queries a single score is uncertain by about ±18 points; with 150, by about ±8.

## Results

Hits@10 / MRR, hybrid unless stated.

| Run | known-item | paraphrase | deep | chat | French | all 150 |
|---|---|---|---|---|---|---|
| E1, today (keyword alone) | 15 / 0.35 | 0 / 0.00 | 14 / 0.37 | 9 / 0.27 | 9 / 0.36 | 47 / 0.27 |
| E1, today (semantic alone) | 31 / 0.70 | 12 / 0.16 | 1 / 0.00 | 13 / 0.31 | 10 / 0.30 | 67 / 0.31 |
| **E1, today (hybrid)** | 29 / 0.63 | 13 / 0.17 | 14 / 0.32 | 14 / 0.38 | 12 / 0.45 | **82 / 0.40** |
| E2, whole texts in passages | 29 / 0.64 | 10 / 0.10 | 17 / 0.39 | 14 / 0.31 | 13 / 0.43 | 83 / 0.38 |
| E5, whole-chat vectors only | 29 / 0.63 | 15 / 0.22 | 14 / 0.32 | 9 / 0.21 | 12 / 0.45 | 79 / 0.37 |
| E5, rounds plus whole-chat vectors | 29 / 0.63 | 12 / 0.14 | 14 / 0.32 | 14 / 0.38 | 12 / 0.45 | 81 / 0.39 |
| E5, rounds without the chat title | 29 / 0.62 | 15 / 0.18 | 14 / 0.32 | 18 / 0.41 | 12 / 0.45 | 88 / 0.40 |
| E3, `all-MiniLM-L6-v2`, lean corpus | 29 / 0.63 | 13 / 0.17 | 14 / 0.31 | 14 / 0.38 | 13 / 0.45 | 83 / 0.40 |
| E3, `bge-small-en-v1.5`, lean corpus | 30 / 0.68 | 10 / 0.17 | 14 / 0.29 | 18 / 0.46 | 16 / 0.56 | 88 / 0.44 |
| E6, reranker on the start | 31 / 0.87 | 13 / 0.20 | 6 / 0.10 | 15 / 0.37 | 13 / 0.52 | 78 / 0.42 |
| E6, reranker on start and best window | 31 / 0.86 | 13 / 0.21 | 15 / 0.42 | 15 / 0.33 | 13 / 0.52 | 87 / 0.48 |
| **E5 + E6, untitled rounds and window reranker** | 31 / 0.86 | 13 / 0.21 | 15 / 0.42 | 17 / 0.37 | 13 / 0.52 | **89 / 0.49** |

### E2, coverage

- 67,000 extra passages (900 characters, every 750) cover the text past each item's first 500 characters and each round's first 1,000.
- Semantic search finds 11 of 30 deep queries, up from 1 (10 better, 0 worse; p = 0.002).
- But passages from long items push short targets down: paraphrase drops 13 → 10 in hybrid.
- Net hybrid gain: one query. Keyword search already covers deep text, so hybrid gains little.
- It cost 36 minutes to embed 160,000 units at 74 per second.

### E3, model (partial)

- **The lean corpus:** every model is compared on today's units without the 61,000 title-only watch-history videos. No query targets one, and dropping them changes today's result by one query (82 → 83).
- **`bge-small-en-v1.5`** (33M parameters, 384 dimensions, MIT licence):
  - semantic 68 → 77, hybrid 83 → 88, MRR 0.40 → 0.44;
  - known items 0.82 MRR in semantic mode, French 13 → 16;
  - embeds at 30 units a second against MiniLM's 37 on the same busy machine.
- **Still running:** `granite-embedding-small-english-r2`, `embeddinggemma-300m` and `Qwen3-Embedding-0.6B`.

### E5, chats

- **Rounds stay the unit.** Whole-chat vectors alone lose 5 chat queries (14 → 9; 8 worse, 0 better; p = 0.008). Adding them beside rounds changes nothing.
- **Leaving the chat's title out of each round's vector helps:** chat 14 → 18, all 82 → 88. With the title, every round of a chat looks alike, and the title outweighs the round.

### E6, ranking

- **Fusion weights:** equal weights are best. Shifting weight to semantic loses 13 queries at once; a cross-validated weight picks equal weights again.
- **A third list,** E2's passages fused as their own list, is worse: 77 of 150.
- **Reranker:** `cross-encoder/ms-marco-MiniLM-L6-v2` reorders the hybrid top 30.
  - Reading only each candidate's first 1,500 characters lifts known items (MRR 0.63 → 0.87; 14 better, 0 worse) but loses deep queries (14 → 6).
  - Also reading the 1,500 characters with most query words fixes that: all 82 → 87, MRR 0.40 → 0.48 (31 better, 14 worse; p = 0.016).
  - It costs about 3–4 seconds per query on pop-mini's CPU, measured while other jobs ran. That needs a clean measurement before it could go live.

### Not run

- **E4, units:** waits for E3, because the best passage size depends on the model's input length.
- **E7, related items:** has 36 seed items with judged pairs ready, not yet run.
- **E8, LLM context:** needs the other machine.
- **E9, images:** not started.

## Industry state of the art, for comparison

Details and sources are in [`embedding-research.md`](embedding-research.md), "Reference scores".

| Benchmark | What it measures | Typical results |
|---|---|---|
| BEIR, 18 public sets | ranking quality (nDCG@10) | strong small models about 50–55; BM25 a strong baseline |
| MS MARCO passages | MRR@10, one answer per web query | BM25 0.18; best dense and reranked systems about 0.39–0.40 |
| LongMemEval | finding the right part of a long personal chat history (Recall@10) | rounds 0.69–0.78; sessions 0.78–0.86 |
| Anthropic contextual retrieval | its own documents (Recall@20) | 94% plain; 98% with context, BM25 and reranking |

- **Closest to LeStash:** LongMemEval, with 70–86% of targets in the top 10. We're at 59% overall at best, and 60% on chat queries (18 of 30).
- **Our order of gains matches the literature's:** hybrid first, then a reranker, then a better model or units.
- **Our set is hard by design:** it was built from paraphrases, deep passages and cross-language queries.

## Against the targets

| Kind | Target (hybrid) | Today | Best so far |
|---|---|---|---|
| known-item | 90%+, MRR 0.8+ | 83%, 0.63 | 89%, 0.86 (reranker) |
| deep | 70%+ | 47% | 57% (E2) |
| paraphrase | 60–70% | 43% | 50% (E5) |
| chat | 70% | 47% | 60% (E3 bge, E5) |
| French | 60%+ | 48% | 64% (E3 bge) |
| all | 75%+ | 55% | 59% |

## Decisions for Matthew

- **Untitled rounds:** cheap. It means re-embedding 12,000 rounds in about 7 minutes. It's a clear gain on chats with no loss elsewhere. Proposal: do it.
- **Model:** wait for the rest of E3, then pick the smallest model within noise of the best. `bge-small-en-v1.5` is the candidate so far.
- **Reranker:** the largest single gain on known items, but seconds per query on CPU. Proposals:
  - time it cleanly, with the top 20 instead of 30;
  - or offer it as a "best match" option rather than for every search.
- **Title-only watch-history videos:** leaving them out of semantic search loses nothing measurable. Proposal: leave them out, as the design already suggested.
- **Test set:** review the 150 queries before any of these goes live, since all the numbers rest on them.

## Evidence

- **Location:** on pop-mini, in `~/lestash-evidence/2026-11-13-search-test-set/`, not in the repository.
- **The queries:** `queries.toml` and the review sheet `review.md`.
- **Each run:** `exp/<run>/`, with results, scores and query-by-query comparisons. E1 and E2 also have screenshots of every query in the app.
- **Tools:** the harness in `tools/exp/` (vector builder, test server, E5 and E6 scripts), the recorder, the scorer and the comparison script.
- **The DB copy and vectors:** `~/lestash-test/search-exp/`.
