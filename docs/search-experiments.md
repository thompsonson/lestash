# Search experiments, November 2026

Results of the search experiments planned in [`embeddings-design.md`](embeddings-design.md) ("Experiments", "Targets"), for #221. Run on 13 Nov 2026 against a copy of the live database (1.101.1): searches on pop-mini, the two largest models embedded on computer.

## Summary

- **Hybrid search is the right default.** It beats keyword and semantic search alone on every kind of query but known items, where semantic alone is slightly ahead.
- **Best result: `embeddinggemma-300m`, with chat rounds embedded without the chat's title.**
  - Hybrid finds 100 of 150 in the top 10, with MRR 0.52, against 82 and 0.40 today.
  - Query by query, against today: 37 better, 19 worse (p = 0.022).
  - Chat queries reach their target: 21 of 30. French goes beyond it: 19 of 25.
  - A query takes 55 ms to embed on pop-mini.
- **Each change on its own:**
  - Untitled rounds: chat queries 14 → 18 of 30 with today's model, and 18 → 21 with EmbeddingGemma.
  - A better model: of five tested, EmbeddingGemma is best (97 of 150), then Qwen3-0.6B (93), Granite (91), bge-small (88) and today's MiniLM (83).
  - A reranker: worth MRR 0.40 → 0.48 with today's model. With EmbeddingGemma it adds only 0.52 → 0.54, at about 1.3 s per query.
- **Didn't pay:**
  - Embedding whole texts in passages (E2) finds deep passages, but crowds out short items: hybrid 82 → 83.
  - Whole-chat vectors lose to rounds.
- **Still short of the targets** (75% overall): the best run finds 67%. Deep text (14 of 30) and paraphrase (16 of 30) are the weakest kinds.

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
| E5 + E6, untitled rounds and window reranker | 31 / 0.86 | 13 / 0.21 | 15 / 0.42 | 17 / 0.37 | 13 / 0.52 | 89 / 0.49 |
| E3, `granite-embedding-small-english-r2`, lean | 31 / 0.67 | 12 / 0.18 | 16 / 0.36 | 15 / 0.41 | 17 / 0.53 | 91 / 0.44 |
| E3, `Qwen3-Embedding-0.6B`, lean | 30 / 0.69 | 13 / 0.23 | 14 / 0.36 | 18 / 0.46 | 18 / 0.67 | 93 / 0.48 |
| E3, `embeddinggemma-300m`, lean | 31 / 0.72 | 16 / 0.29 | 13 / 0.30 | 18 / 0.46 | 19 / 0.70 | 97 / 0.50 |
| **E3 + E5, EmbeddingGemma, untitled rounds** | 30 / 0.72 | 16 / 0.27 | 14 / 0.30 | 21 / 0.58 | 19 / 0.69 | **100 / 0.52** |
| E3 + E5 + E6, the same plus the window reranker | | | | | | 98 / 0.54 |

### E2, coverage

- 67,000 extra passages (900 characters, every 750) cover the text past each item's first 500 characters and each round's first 1,000.
- Semantic search finds 11 of 30 deep queries, up from 1 (10 better, 0 worse; p = 0.002).
- But passages from long items push short targets down: paraphrase drops 13 → 10 in hybrid.
- Net hybrid gain: one query. Keyword search already covers deep text, so hybrid gains little.
- It cost 36 minutes to embed 160,000 units at 74 per second.

### E3, model

- **The lean corpus:** every model is compared on today's units without the 61,000 title-only watch-history videos. No query targets one, and dropping them changes today's result by one query (82 → 83).
- Each model uses its own query and document prompts (bge's recommended query instruction; Qwen3's and EmbeddingGemma's built-in prompts).

| Model | Size | Dims | Hybrid hits / MRR | vs MiniLM: better, worse (p) | Query on pop-mini | Embedding speed |
|---|---|---|---|---|---|---|
| `all-MiniLM-L6-v2` (today) | 22M | 384 | 83 / 0.40 | — | 7 ms | 37/s on pop-mini |
| `bge-small-en-v1.5` | 33M | 384 | 88 / 0.44 | 27, 18 (0.23) | 11 ms | 30/s on pop-mini |
| `granite-embedding-small-english-r2` | 47M | 384 | 91 / 0.44 | 28, 17 (0.14) | 16 ms | 4/s on a busy pop-mini |
| `Qwen3-Embedding-0.6B` | 600M | 1,024 | 93 / 0.48 | 34, 12 (0.002) | 400 ms | 19/s on computer |
| `embeddinggemma-300m` | 308M | 768 | 97 / 0.50 | 34, 15 (0.009) | 55 ms | 23/s on computer |

- **EmbeddingGemma is the pick:**
  - the best hybrid result;
  - fast enough per query on pop-mini;
  - strongest on French, 19 of 25, as it is multilingual.
- **Its terms:** it is under Google's Gemma terms (accepted 13 Nov 2026) rather than an open-source licence.
- **Qwen3-0.6B is close, but too slow:** 400 ms per query on pop-mini is too slow for every search.
- Embedding speeds were measured on machines busy with other runs; treat them as rough.

### E5, chats

- **Rounds stay the unit.** Whole-chat vectors alone lose 5 chat queries (14 → 9; 8 worse, 0 better; p = 0.008). Adding them beside rounds changes nothing.
- **Leaving the chat's title out of each round's vector helps:** chat 14 → 18, all 82 → 88. With the title, every round of a chat looks alike, and the title outweighs the round.

### E6, ranking

- **Fusion weights:** equal weights are best. Shifting weight to semantic loses 13 queries at once; a cross-validated weight picks equal weights again.
- **A third list,** E2's passages fused as their own list, is worse: 77 of 150.
- **Reranker:** `cross-encoder/ms-marco-MiniLM-L6-v2` reorders the hybrid top 30. Its gain shrinks as the model improves: with EmbeddingGemma and untitled rounds, MRR 0.52 → 0.54 and hits 99 → 98.
  - Reading only each candidate's first 1,500 characters lifts known items (MRR 0.63 → 0.87; 14 better, 0 worse) but loses deep queries (14 → 6).
  - Also reading the 1,500 characters with most query words fixes that: all 82 → 87, MRR 0.40 → 0.48 (31 better, 14 worse; p = 0.016).
  - Cost on pop-mini, measured quietly: 0.5 s for 20 pairs, 0.7 s for 30, 1.5 s for 60. The two-window version reads 60.

### Not run

- **E4, units:** next. EmbeddingGemma reads up to 2,048 tokens, so longer units may help the deep queries that are still weak.
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

- **Closest to LeStash:** LongMemEval, with 70–86% of targets in the top 10. Our best run finds 67% overall, and 70% of chat queries (21 of 30).
- **Our order of gains matches the literature's:** hybrid first, then a reranker, then a better model or units.
- **Our set is hard by design:** it was built from paraphrases, deep passages and cross-language queries.

## Against the targets

| Kind | Target | Today | Best run (EmbeddingGemma, untitled rounds) | Best of any run |
|---|---|---|---|---|
| known-item | 90%+, MRR 0.8+ | 83%, 0.63 | 86%, 0.72 | 89%, 0.86 (reranker) |
| deep | 70%+ | 47% | 47% | 57% (E2) |
| paraphrase | 60–70% | 43% | 53% | 53% |
| chat | 70% | 47% | **70%** | 70% |
| French | 60%+ | 48% | **76%** | 76% |
| all | 75%+ | 55% | 67% | 67% |

## Decisions for Matthew

- **Switch to EmbeddingGemma, with untitled rounds.** Proposal: do it.
  - The vector table goes from 384 to 768 numbers per vector.
  - Every unit is re-embedded: about 25 minutes for the lean set on computer, or about an hour and a half with all of today's 93,000 units.
- **Reranker:** not worth its 1–1.5 s per query once EmbeddingGemma is in. Proposal: leave it out; revisit as a "best match" option if known items still disappoint.
- **Deep text:** the weakest kind left. Proposal: run E4 with EmbeddingGemma's longer input before deciding on passages.
- **Title-only watch-history videos:** leaving them out of semantic search loses nothing measurable. Proposal: leave them out, as the design already suggested.
- **Test set:** review the 150 queries before any of these goes live, since all the numbers rest on them.

## Evidence

- **Location:** on pop-mini, in `~/lestash-evidence/2026-11-13-search-test-set/`, not in the repository.
- **The queries:** `queries.toml` and the review sheet `review.md`.
- **Each run:** `exp/<run>/`, with results, scores and query-by-query comparisons. E1 and E2 also have screenshots of every query in the app.
- **Tools:** the harness in `tools/exp/` (vector builder, test server, E5 and E6 scripts), the recorder, the scorer and the comparison script.
- **The DB copy and vectors:** `~/lestash-test/search-exp/`.
- **On computer:** the environment and logs used for EmbeddingGemma and Qwen3 are in `~/lestash-embed/`.
