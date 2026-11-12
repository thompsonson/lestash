# Representing LeStash items for retrieval and related items — research report

Date: 12 Nov 2026. Scope: web and paper research only; no code changed. Companion to [`embeddings-design.md`](embeddings-design.md).

## How to read this report

- **[M]** means measured evidence from a paper or public benchmark.
- **[V]** means a vendor or model-card claim, often self-reported.
- **[J]** means my judgement, not a sourced result.
- Numbers are copied from the cited source; check the source before quoting them on.
- I could not load the live MTEB leaderboard (it is a JavaScript app). Model scores come from model cards and papers instead.

## Current state in LeStash (from the code)

- `packages/lestash/src/lestash/core/embeddings.py` uses `all-MiniLM-L6-v2` with 384 dimensions.
- It embeds `title + content[:500]` per parent item into a `vec0` table (sqlite-vec).
- `packages/lestash-server/src/lestash_server/routes/embeddings.py` builds the same text.
- FTS5 already exists, so BM25 is available today for a hybrid.

---

## 1. How large systems carve up and represent documents

### 1.1 Web search engines

- **Google passage ranking.** Announced Oct 2020; it judges individual passages, not only whole pages. [V] ([Google blog](https://blog.google/products/search/search-on/))
  - Google said it would affect 7% of queries once rolled out globally. [V]
  - It went live for US English on 10 Feb 2021. ([Search Engine Land](https://searchengineland.com/google-passage-ranking-now-live-in-us-english-search-results-346034))
  - Google still indexes whole pages; passages are an extra ranking signal. ([Search Engine Land](https://searchengineland.com/google-passage-ranking-347735))
  - Lesson [J]: the unit you *index* and the unit you *score* can differ.
- **Bing** applied BERT to every query globally from 2019. [V] ([Azure blog](https://azure.microsoft.com/en-us/blog/bing-delivers-its-largest-improvement-in-search-experience-using-azure-gpus/))
  - That model is a ranker over candidates, not the first-stage index. [J]
- **Multi-field documents (BM25F).** Robertson, Zaragoza and Taylor (CIKM 2004) weight term frequencies per field *before* saturation. [M] ([paper record](https://ir.webis.de/anthology/2004.cikm_conference-2004.6))
  - Summing per-field BM25 scores breaks BM25's saturation curve, which is why BM25F exists.
  - A title with weight 2 behaves like the title repeated twice.
- **Anchor text** is text *other* pages write about a page. ([Craswell, Hawking, Robertson, SIGIR 2001](https://ir.webis.de/anthology/2001.sigirconf_conference-2001.31))
  - It gave a large boost for site-finding tasks. [M]
  - Lesson [J]: what others say about an item is a strong field. LeStash has analogues: comments, link text, highlights, NotebookLM summaries.

### 1.2 Document expansion

- **doc2query / docTTTTTquery** appends predicted questions to each passage, then indexes with BM25. ([Nogueira & Lin 2019](https://cs.uwaterloo.ca/~jimmylin/publications/Nogueira_Lin_2019_docTTTTTquery-v2.pdf))
  - MS MARCO passage dev MRR@10: BM25 0.184 → docTTTTTquery 0.265 (paper) or 0.277 (repo reproduction). [M] ([repo](https://github.com/castorini/docTTTTTquery))
  - Cost is paid once at index time; query time stays BM25-cheap.

### 1.3 Learned sparse and late interaction

- **SPLADE** learns sparse term weights, including expansion terms. [M]
  - distilSPLADE v2: MS MARCO MRR@10 36.8, BEIR avg nDCG@10 49.9. ([Naver SPLADE page](https://europe.naverlabs.com/research/machine-learning-for-robotics/splade-models))
  - A Sept 2026 study of 33 retrievers found SPLADE-v3 near the best at much lower latency. [M] ([Velev & Spitz 2026](https://arxiv.org/abs/2609.29455))
- **ColBERT** keeps one vector per token and scores with MaxSim summed over query tokens. ([Khattab & Zaharia 2020](https://arxiv.org/abs/2004.12832))
  - ColBERTv2 reached 39.7 MRR@10 on MS MARCO dev. [M] ([ColBERTv2](https://arxiv.org/abs/2112.01488))
  - Residual compression cuts storage to 20–36 bytes per token vector. [M]
  - Storage is still one vector per *token*, which is heavy for long documents. [J]
- BEIR found BM25 a robust zero-shot baseline; rerankers and late interaction were best but costly. [M] ([BEIR, Thakur et al. 2021](https://arxiv.org/abs/2104.08663))

### 1.4 Long documents: passages and aggregation (FirstP / MaxP / SumP)

- Dai & Callan (SIGIR 2019) split documents into overlapping passages and score each. ([paper](https://arxiv.org/abs/1905.09217))
  - FirstP uses the first passage; MaxP the best passage; SumP the sum.
  - ClueWeb09 passages: 150-word window, 75-word stride.
  - Robust04 nDCG@20, description queries: BOW 0.409, FirstP 0.491, MaxP 0.529, SumP 0.524. [M]
  - Title queries: FirstP 0.444, MaxP 0.469, SumP 0.467. [M]
  - On ClueWeb09 description queries FirstP was best (0.272). [M] So the best aggregator is corpus-dependent.
  - Natural-language queries gained more than keyword queries. [M]

### 1.5 Chunking: fixed vs semantic vs LLM

- **"Is Semantic Chunking Worth the Computational Cost?"** (Qu, Tu, Bao, 2024). ([arXiv 2410.13070](https://arxiv.org/abs/2410.13070))
  - On real (non-stitched) datasets, fixed-size was equal or better. HotpotQA F1@5: fixed 90.59, breakpoint 87.37. [M]
  - Semantic chunking won only on artificially stitched, topic-mixed documents. NQ*: 43.79 → 63.93. [M]
  - Evidence retrieval and answer quality barely differed. [M]
  - The embedding model mattered more than the chunker. [M]
- **Chroma, "Evaluating Chunking Strategies for Retrieval"** (2024). ([report](https://www.trychroma.com/research/evaluating-chunking))
  - 472 synthetic queries over 5 corpora, including a chat-log corpus. Token-level recall, precision and IoU. [M]
  - With text-embedding-3-large, recall ranged 83.6 (Kamradt semantic) to 91.9 (LLM chunker). [M]
  - Recursive splitter, 200 tokens, no overlap, was consistently strong. [M]
  - The OpenAI default (800 tokens, 400 overlap) had the lowest precision and IoU. [M]
  - Overlap lowers efficiency (IoU) without clear recall gains. [M]
  - With all-MiniLM-L6-v2 the best recall was only 82.4. [M]
  - Caveat: small, LLM-generated query set; standard deviations near ±30. [M]
- **Judgement [J].** The evidence does not support paying for semantic chunking. Structure-aware splitting (headings, paragraphs, turns) is cheap and keeps meaningful units; fixed-size within a structural unit is the safe default.

### 1.6 Context-aware chunk embeddings

- **Anthropic Contextual Retrieval** (Sept 2024). ([post](https://www.anthropic.com/engineering/contextual-retrieval))
  - An LLM writes 50–100 tokens situating each chunk in its document. The text is prepended before embedding and BM25 indexing.
  - Failure rate (1 − recall@20): 5.7% baseline → 3.7% contextual embeddings → 2.9% plus contextual BM25 → 1.9% plus reranking. [V, measured by vendor]
  - Those are 35%, 49% and 67% reductions. Reranking took top-150 to top-20 with Cohere's reranker. [V]
  - Cost was about $1.02 per million document tokens with prompt caching. [V]
  - Locally this needs an LLM pass over every chunk; on CPU that is slow. [J]
- **Late chunking** (Jina, 2024). ([arXiv 2409.04701](https://arxiv.org/abs/2409.04701))
  - Encode the whole document first, then mean-pool token vectors per chunk. No LLM is needed.
  - BEIR nDCG@10 relative gain over naive chunking: 3.5% (fixed), 3.6% (sentence), 2.7% (semantic). [M]
  - Example: jina-v3 TREC-COVID 73.0 → 77.2. nomic-v1 SciFact was flat (70.7 → 70.6). [M]
  - Gains grow with document length and shrink with chunk size. [M]
  - It needs a long-context model (8k+ tokens) and token-level output. [J]

### 1.7 Proposition-level indexing

- **Dense X Retrieval** (Chen et al., EMNLP 2024). ([arXiv 2312.06648](https://arxiv.org/abs/2312.06648))
  - Propositions are atomic, self-contained factoids, made by a fine-tuned Flan-T5-large.
  - Avg Recall@5, passage → proposition: SimCSE 34.3 → 46.3; Contriever 43.0 → 52.7. [M]
  - Supervised retrievers gained less: DPR 57.3 → 59.9; GTR 65.2 → 68.0. [M]
  - Index size grows a lot: 41M passages became 257M propositions. [M]
  - Judgement [J]: modern supervised embedders gain little; storage grows about 6×. A fit for factual notes, not for chats or posts.

### 1.8 Hierarchical / summary indexing

- **RAPTOR** (ICLR 2024) clusters chunks, summarises clusters recursively, and indexes all levels. ([ICLR page](https://iclr.cc/virtual/2024/poster/19034))
  - "Collapsed tree" search over all levels beat level-by-level traversal. [M]
  - With GPT-4 it set QuALITY state of the art, +20 points absolute. [V/M, authors' claim]
  - It needs many LLM summary calls per document. [J]
- **Parent-document retrieval** matches on small chunks and returns the larger parent. ([LangChain docs](https://python.langchain.com/docs/how_to/parent_document_retriever))
  - It is an engineering pattern; I found no benchmark for it. [J]
  - LeStash already has parent/child items, so this maps directly. [J]

### 1.9 Conversations: session vs turn granularity

- **LongMemEval** (Wu et al., ICLR 2025). ([arXiv 2410.10813](https://arxiv.org/abs/2410.10813))
  - Retriever: Stella V5 1.5B. Keys used only user-side utterances.
  - Round (one user turn plus reply) gave better QA with GPT-4o than whole sessions. [M]
  - Summaries or extracted facts *as values* lost information and lowered QA. [M]
  - Raw text plus extracted user facts as the *key* was best. [M]
  - LongMemEval-M Table 3, Recall@10: round 0.692 → 0.784 with facts; session 0.783 → 0.862 with facts. [M]
  - Session-level recall was higher than round-level, because there are fewer, larger targets. [M]
  - Temporal questions: session R@5 0.639 vs round 0.421; time-aware query expansion helps both. [M]
  - Reported average gains from key expansion: +9.4% recall, +5.4% QA accuracy. [M]

---

## 2. What is recognised as working

- **Hybrid lexical + dense** is the most consistent win. [M]
  - BEIR shows BM25 strong out of domain. ([BEIR](https://arxiv.org/abs/2104.08663))
  - Anthropic: adding BM25 took failures from 3.7% to 2.9%. [V]
- **RRF** (Cormack et al., SIGIR 2009) fuses by rank, with k = 60. ([paper](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf))
  - MAP: RRF 0.2145 vs best single system 0.2016 and Condorcet 0.2074. [M]
- **Convex combination** of normalised scores beat RRF in- and out-of-domain. ([Bruch et al. 2023](https://arxiv.org/abs/2210.11934))
  - It needs only a few labelled queries to tune one weight. [M]
  - RRF was more parameter-sensitive than assumed. [M]
  - Judgement [J]: start with RRF (no labels), switch to a tuned convex combination once a test set exists.
- **Cross-encoder rerankers** are the biggest single quality step after hybrid. [M]
  - `ms-marco-MiniLM-L6-v2`: TREC DL19 nDCG@10 74.30, MS MARCO MRR@10 39.01. ([model card](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2))
  - It runs 1,800 docs/s on a V100 GPU; L12 adds nothing (74.31) at half speed. [V]
  - I found no authoritative CPU timings. [J] Estimate: MiniLM-L6 on 16 threads reranks 50 short passages in roughly 0.1–0.5 s.
  - bge-reranker-v2-m3 (568M) is likely several seconds per 100 pairs on CPU. [J] Measure before adopting.
  - ms-marco models are English-only. [J]
- **Embedding quality beats chunking cleverness.** Qu et al. found the embedder choice mattered more than the chunker. [M] Chroma's MiniLM numbers trail text-embedding-3-large by about 9 recall points. [M]

---

## 3. CPU-friendly embedding models (late 2026)

Scores are from model cards unless noted. MTEB versions differ between rows, so compare within a column source only.

| Model | Params | Max tokens | Dims (MRL) | Languages | Licence | Retrieval score (source) |
|---|---|---|---|---|---|---|
| all-MiniLM-L6-v2 (current) | 22M | 256 | 384 (no) | English | Apache-2.0 | MTEB eng v2 retr. ~42.9 ([snapshot](https://supermemory.ai/blog/best-open-source-embedding-models-benchmarked-and-ranked/)) |
| bge-small-en-v1.5 | 33M | 512 | 384 (no) | English | MIT | MTEB retr. 51.68 ([card](https://huggingface.co/BAAI/bge-small-en-v1.5)) |
| bge-base-en-v1.5 | 109M | 512 | 768 (no) | English | MIT | MTEB retr. 53.25 ([card](https://huggingface.co/BAAI/bge-small-en-v1.5)) |
| granite-embedding-small-english-r2 | 47M | 8,192 | 384 | English | Apache-2.0 | MTEB-v2 retr. 53.9; LongEmbed 61.9 ([card](https://huggingface.co/ibm-granite/granite-embedding-english-r2)) |
| granite-embedding-english-r2 | 149M | 8,192 | 768 | English | Apache-2.0 | MTEB-v2 retr. 56.4; LongEmbed 67.8 ([card](https://huggingface.co/ibm-granite/granite-embedding-english-r2)) |
| gte-modernbert-base | 149M | 8,192 | 768 | English | Apache-2.0 | MTEB-v2 retr. 57.0; LongEmbed 57.0 (Granite card table) |
| nomic-embed-text-v1.5 | ~0.1B | 8,192 | 768 (to 64) | English | Apache-2.0 | MTEB avg 62.28 at 768, 61.04 at 256 ([card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)) |
| nomic-embed-text-v2-moe | 475M (305M active) | 512 | 768 (to 256) | ~100 | Apache-2.0 | BEIR 52.86, MIRACL 65.80 ([card](https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe)) |
| snowflake-arctic-embed-m-v2.0 | 305M | 8,192 | 768 (yes) | 74 | Apache-2.0 | BEIR 55.40 (Nomic card); MTEB-v2 retr. 58.4 (Granite card) |
| EmbeddingGemma-300m | 308M | 2,048 | 768 (512/256/128) | 100+ | Gemma terms | MTEB eng v2 mean 69.67 at 768, 68.37 at 256 ([card](https://huggingface.co/google/embeddinggemma-300m)) |
| Qwen3-Embedding-0.6B | 600M | 32k | 1,024 (32–1,024) | 100+ | Apache-2.0 | MTEB eng v2 retr. 61.83; multilingual retr. 64.64 ([card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)) |
| jina-embeddings-v3 | 570M | 8,192 | 1,024 (to 32) | multi | CC-BY-NC-4.0 | Non-commercial licence ([card](https://huggingface.co/jinaai/jina-embeddings-v3)) |
| granite-embedding-multilingual-r2 | 311M / 97M | 32,768 | MRL (full model) | 200+ | Apache-2.0 | Scores in paper; not checked ([arXiv 2605.13521](https://arxiv.org/abs/2605.13521)) |

Notes:

- The bge-small to MiniLM gap is about 10 nDCG points across sources. [M] ([mxbai-edge-colbert report](https://arxiv.org/pdf/2510.14880))
- EmbeddingGemma ranked first under 500M params on MTEB multilingual, English and code at release. [V] ([paper](https://arxiv.org/abs/2509.20354))
- EmbeddingGemma does not support float16; use float32 or bfloat16. [V]
- EmbeddingGemma QAT Q8_0 kept 69.49 vs 69.67 English mean. [V]
- Qwen3-0.6B is about 2× EmbeddingGemma's size and 27× MiniLM's. [J]
- English-only models (MiniLM, bge, granite-english, gte-modernbert) will be weak on French text. [J]

### CPU throughput

- I found no authoritative CPU table for these models. [J] Measure on the Ryzen box.
- Sentence-Transformers: ONNX backend gave 1.4–3× speedups. [V] ([efficiency docs](https://sbert.net/docs/sentence_transformer/usage/efficiency.html))
- OpenVINO static int8 processed 4.78× more texts per second, losing 0.36% on average. [V]
- The docs advise benchmarking on your own hardware; llama.cpp won on cloud CPUs. [V]
- Rough estimate [J]: cost scales with params × tokens, and attention grows with sequence length.
  - A 300M model is about 10× slower per token than a 33M model.
  - Long-context embedding (8k) costs much more than eight 1k passes, because attention is quadratic.

### Vector quantisation

- int8 vectors kept about 97–99% of retrieval quality; binary with rescoring about 96%. [M] ([HF blog](https://huggingface.co/blog/embedding-quantization))
- Binary is 32× smaller; int8 is 4× smaller. [M]
- e5-base-v2 lost heavily under binary quantisation, so results are model-specific. [M]
- At LeStash scale (~200k vectors × 768 × 4 bytes ≈ 0.6 GB), quantisation is optional. [J]

---

## 4. Related items in practice

- **Lexical "more like this"** (Lucene/Elasticsearch) picks the top tf-idf terms of a document and runs them as an OR query. ([Elastic docs](https://www.elastic.co/guide/en/elasticsearch/reference/master/query-dsl-mlt-query.html))
  - It is cheap, explainable, and already possible with FTS5. [J]
- **Document-level vectors from chunk vectors.**
  - A 2026 paper (DICE) aggregates chunk embeddings into one document vector and improves long-document retrieval on LongEmbed. ([arXiv 2606.18781](https://arxiv.org/abs/2606.18781)) Its abstract does not say which pooling it uses; the claim that mean beats max came from a summary site and is unverified.
  - Best-pair (MaxP-style) finds "shares one strong passage". Mean finds "about the same overall topic". [J]
  - These answer different questions; both are worth offering. [J]
- **Collaborative / co-activity signals.**
  - Item2Vec learns item similarity from co-occurring item sets, without user IDs. [M] ([Barkan & Koenigstein](https://arxiv.org/abs/1603.04259))
  - It was competitive with item-based SVD. [M]
  - LeStash analogues [J]: same session, same day, same channel, same person, explicit Links, same Collection.
  - With one user, co-activity is sparse but highly personal. [J]
- **Personal / lifelog retrieval.**
  - The Lifelog Search Challenge started with known-item search as its core task. ([LSC review](https://arxiv.org/html/2506.06743v2))
  - LSC systems moved toward embedding plus LLM retrieval in 2022–2024. [M]
  - Personal data is noisy, so iterative, interactive search matters. [M]
  - Few shared personal test sets exist, because of privacy. [M] So you build your own.
  - Re-finding uses time, source and person cues heavily. [J] Filters and facets matter as much as vectors.

---

## 5. Evaluation for a small personal corpus

### Test set

- **Known-item queries.** Pick an item; write the query you would type to re-find it. This mirrors LSC's main task. [J]
- **Paraphrase queries.** Same need, no shared words. This tests dense over BM25. [J]
- **Deep queries.** The answer sits beyond the first 500 characters. This tests chunking directly. [J]
- **Turn queries.** The answer is one turn inside a long chat. [J]
- **French queries**, and cross-language ones (French query, English item). [J]
- **Related-item pairs.** For a seed item, mark items you judge related. [J]
- Stratify by item kind, so a gain on posts cannot hide a loss on books. [J]
- About 100–200 queries can rank systems; small sets give wide confidence intervals. [J]

### Metrics

- **Recall@k** (k = 10, 20, 50): did the item reach the candidate set? Use this for first stage.
- **MRR@10**: one known item per query, so rank of the first hit fits well.
- **nDCG@10**: graded relevance, for related items and multi-answer queries.
- Score at **item level**, collapsing chunks to their parent (MaxP-style), as LeStash shows items. [J]
- Report per-kind results and paired bootstrap or paired t-tests between systems. [J]

### Synthetic queries

- Rahmani et al. (SIGIR 2024) found LLM-generated test collections ranked systems like human ones. [M] ([arXiv 2405.07767](https://arxiv.org/abs/2405.07767))
- Heavy filtering was needed: 13 of 48 T5 and 18 of 49 GPT-4 queries were kept. [M]
- A follow-up found synthetic queries longer and lexically different from real ones. [M] ([arXiv 2506.10301](https://arxiv.org/abs/2506.10301))
- Bias hit absolute scores more than relative system rankings. [M]
- Pitfalls [J]:
  - Generated queries copy words from the chunk, which favours BM25 and the chunk they came from.
  - Generating from the same chunking you test biases toward that chunking. Generate from the whole item instead.
  - Using the same LLM family for queries and contextualisation inflates contextual methods.
  - Keep a hand-written subset as the anchor; use synthetic queries for breadth only.

---

## 6. Other considerations

- **Instruction prefixes are required for several models.** Missing them silently lowers quality. [V]
  - nomic: `search_query:` / `search_document:`.
  - EmbeddingGemma: `task: search result | query:` and `title: … | text: …` for documents.
  - Qwen3: `Instruct: {task}\nQuery:` on queries only.
  - bge v1.5: optional query instruction; recommended for short queries over long documents.
- **Normalise** vectors and use cosine or dot product; renormalise after MRL truncation. [V] (EmbeddingGemma card)
- **Store model name, version, prefix and text recipe with each vector.** A model change means a full re-embed. [J]
- **Re-embed cost.** Measure total tokens first; most cost sits in the few 100k–1.6M-character items. [J]
- **Freshness.** Embed on insert or update, keyed by a content hash. LeStash already hashes content. [J]
- **Privacy.** All candidate models run locally; no text leaves the box. [J]
  - Contextual Retrieval and doc2query need an LLM. Local CPU LLMs are slow; a cloud LLM breaks local-only. [J]
- **Licences.** jina-v3 is non-commercial. EmbeddingGemma uses Gemma terms, not OSI. Fine for personal use, but note it. [J]
- **Truncation.** Silent truncation at 256 or 512 tokens is the same failure as today's 500 characters. Log it. [J]

---

## Implications for LeStash

### Representation per item kind

| Item kind | Unit to embed | Extra fields / keys | Rationale |
|---|---|---|---|
| Posts, comments (short) | Whole item, one vector | Author, channel; parent post text for comments | Fits one passage; no chunking needed. A comment alone lacks context, like an orphan chunk. [J] |
| Chats (Claude, Gemini) | One vector per round (user turn + reply) | Chat title prepended; optional session vector | LongMemEval: rounds help QA; sessions give higher recall. Keep both and fuse. [M+J] |
| Long documents, papers, books | Structure-aware passages (~200–400 tokens) within headings; plus one item vector | Title and section heading prepended to each passage (cheap contextualisation) | Fixed vs semantic shows no reliable win. Chroma favours ~200 tokens, no overlap. Score items by MaxP. [M+J] |
| Highlights / annotations | Whole quote, one vector | Book title, chapter, nearby book text if available | Short and proposition-like; the book is the "parent document". [J] |
| Videos | Title + description; transcript passages when present | Channel name | Most have no transcript; few-token items depend on BM25 and metadata. [J] |
| NotebookLM notebooks | Notebook vector from title and sources; artifacts as child items | Audio-overview transcript chunked like documents | The generated summary works like RAPTOR's upper layer. [J] |
| Search history | Query text, one vector | Time, engine | Useful as a link to what was read next, more than as a search target. [J] |

### Search pipeline (proposal) [J]

- First stage: FTS5 BM25 plus dense over passages, fused with RRF (k = 60).
- Collapse passages to items with MaxP; keep the best passage as a snippet.
- Optional second stage: rerank the top 30–50 with a small cross-encoder, if CPU latency allows.
- Filters for kind, source, person and date sit beside the ranking.

### Related items (W4) proposal [J]

- Blend three signals, each shown with its reason.
  - Content: mean-of-chunks item vector, plus best passage pair for "shares a passage".
  - Lexical: MLT-style top tf-idf terms through FTS5.
  - Graph: explicit Links, same person or channel, same Collection, co-activity in a time window.
- Exclude own children and parent from results.

### Experiment set

Run each on the same frozen test set and report per-kind Recall@20, MRR@10 and nDCG@10.

1. **Build the test set.**
   - 150 hand-written queries across kinds: known-item, paraphrase, deep, turn, French.
   - 300 LLM-generated queries from whole items, filtered by hand.
   - 30 seed items with judged related items.
2. **E1 — baselines.** BM25 (FTS5) alone; current MiniLM title+500 alone; RRF of both.
3. **E2 — model swap, same text recipe.** bge-small, granite-small-r2, EmbeddingGemma (256 and 768), Qwen3-0.6B. Record embedding time per 1,000 items on this CPU.
4. **E3 — unit of indexing**, with the best E2 model.
   - Long documents: first-512 vs passages 200/400 tokens vs heading-aware passages.
   - Overlap 0 vs 25%.
   - Aggregation: FirstP vs MaxP vs SumP vs mean.
5. **E4 — chats.** Session vs round vs round + session fused; with and without the chat title prefix.
6. **E5 — cheap context.** Title/heading prefix vs none. Late chunking with a long-context model (granite-r2 or nomic v1.5).
7. **E6 — fusion and rerank.** RRF vs tuned convex combination; with and without MiniLM-L6 cross-encoder at top-30; record p50/p95 latency.
8. **E7 — related items.** Content-only vs content + lexical vs content + lexical + graph, on the 30 seeds.
9. **E8 (optional) — LLM enrichment.** Contextual prefixes or doc2query on one kind only, if a local LLM is acceptable.
- Decide by paired significance per kind, not by the overall average. [J]
- Pick the smallest model within noise of the best, because re-embedding and latency costs recur. [J]
