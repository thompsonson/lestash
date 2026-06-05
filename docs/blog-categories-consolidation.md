# Blog categories — consolidation proposal

> Status: proposal · 2026-05-30 · for [`ux-compose-and-categories-design.md`](ux-compose-and-categories-design.md) §3.1 `categories` field
> Source: scraped `matt.thompson.gr/categories/*` on 2026-05-30
> Method: enumerated every category listed in the archive nav; pulled post counts and titles from each `/categories/<slug>/` page; spot-checked a handful of post pages for category tags.

The blog has accumulated 18 categories over ~15 years, half of which overlap, two of which are stale, and several of which are sub-series of a larger theme. This file proposes a tighter set the composer can offer as autocomplete candidates.

---

## 1. Current categories (verbatim)

Slug — label — approx. post count (from listing pages; capped where the page stopped scrolling):

- `being-human` — Being Human — ~21
- `agenticai` — Agentic AI — ~24
- `intelligent-agents` — Intelligent Agents — ~23
- `vibe-engineering` — Vibe Engineering — ~10
- `ia-series` — IA Series — 11
- `learning` — Learning — 30+
- `deep-learning` — Deep Learning — 8
- `research` — Research — 5
- `agi` — AGI — 10
- `reinforcement-learning` — Reinforcement Learning — 12
- `ml` — ML — 3
- `responsibleai` — Responsible AI — 6
- `being-human-series` — Being Human Series — 6
- `python-series` — Python Series — 1
- `nn-series` — NN Series — 5
- `rl-series` — RL Series — 2
- `powershell` — Powershell — 7
- `projecteuler` — ProjectEuler — 7 (same 7 posts as `powershell`)

---

## 2. Problems with the current set

1. **Slug inconsistency.** `agenticai` and `responsibleai` are squashed; everything else is kebab-case. `projecteuler` is also squashed. Search, autocomplete and URL hygiene all suffer.
2. **Series-vs-theme duplication.** `being-human` (21) and `being-human-series` (6) are the same topic — the series posts are tagged with both. Same shape for `intelligent-agents` (23) vs `ia-series` (11), and `reinforcement-learning` (12) vs `rl-series` (2). The series acts as a sub-filter, not a separate theme — three categories doing the work of one.
3. **Total overlap, no successor.** `powershell` and `projecteuler` contain the **same** 7 posts from 2011. A 15-year-old throwaway sub-project gets two categories; neither has had a new post since 2011.
4. **One-off categories.** `python-series` has 1 post (April 2025). `nn-series` has 5 (Feb-Mar 2025). `rl-series` has 2 (Jan-Feb 2025). These are abandoned mini-courses, not categories — they fit under broader learning/ML buckets.
5. **`ml` is a stub.** 3 posts, all of which are already in `learning` or `deep-learning`. Adds no signal.
6. **No category for personal / family content.** "Sophie's work" is the named use case in the design doc; there is currently no public category that fits a child's school work or other family posts. The author would either need to invent one or leave it uncategorised.
7. **No category for "tools / craft / dev workflow."** Posts like *Building Handy on an Intel Mac*, *Domain Driven Design*, *Modern Python Package Management* sit under `learning` or `vibe-engineering` by default — neither captures "this is a how-to / tool note."

---

## 3. Proposed consolidated set

Target: 11 top-level categories. Every existing category either maps to one of these or retires (§4).

- **`being-human` — Being Human**
  - Reflective / philosophical posts on agency, learning, parenting, society.
  - Absorbs: `being-human`, `being-human-series`
  - Example saved item: a Bluesky thread the author quote-replies with a personal take.

- **`intelligent-agents` — Intelligent Agents**
  - Long-running theory thread on agents, rationality, PEAS, planning. The "course" the author is working through.
  - Absorbs: `intelligent-agents`, `ia-series`
  - Example saved item: arXiv paper on agent design read for the IA series.

- **`agentic-ai` — Agentic AI**
  - Applied / industry-side of agents: coding agents, tools-use, agent products, MCP, deployments.
  - Absorbs: `agenticai`
  - Example saved item: YouTube of a Claude Code demo; LinkedIn post about a new agent framework.

- **`learning` — Learning**
  - The author's own learning notes (Masters, deep-dives, term sheets). The "study journal" bucket.
  - Absorbs: `learning`, `nn-series`, `rl-series`, `python-series` (each series becomes a *tag*, not a category — see §6.1)
  - Example saved item: a textbook chapter summary or a video lecture review.

- **`ml-research` — ML Research**
  - Paper notes, technique deep-dives, model behaviour analysis. The "I read a paper" bucket.
  - Absorbs: `research`, `deep-learning`, `reinforcement-learning`, `ml`, `agi`
  - Example saved item: arXiv paper notes; a Bluesky thread linking to a paper.

- **`responsible-ai` — Responsible AI**
  - Ethics, regulation, safety, alignment commentary. Mostly opinion / commentary.
  - Absorbs: `responsibleai`
  - Example saved item: LinkedIn post about the EU AI Act; news article on AI policy.

- **`vibe-engineering` — Vibe Engineering**
  - The author's named practice: building with LLM agents in the loop. Distinct from `agentic-ai` because it's first-person craft, not third-person commentary.
  - Absorbs: `vibe-engineering`
  - Example saved item: own LeStash PR retrospective; a Cursor / Claude Code workflow note.

- **`software-craft` — Software Craft** *(see §6)*
  - Tool notes, build guides, design patterns, language ergonomics. Not LLM-flavoured.
  - Absorbs: parts of `learning` and `vibe-engineering` that are really "this is how I set up X" or "DDD revisited"
  - Example saved item: a "Building Handy on Intel Mac" how-to; a uv / pipx note.

- **`reading` — Reading** *(see §6)*
  - Book notes, citations, reading-list updates. Today these scatter across `learning` and `being-human`.
  - Absorbs: nothing existing (new)
  - Example saved item: an Audible highlight; a quote from *The Alignment Problem*.

- **`life` — Life** *(see §6)*
  - Family, kids' work, travel, "la rentrée" posts, year-end reviews. The not-work bucket.
  - Absorbs: nothing existing (new) — currently lives uneasily inside `being-human`
  - Example saved item: a photo of Sophie's school project; a Micro.blog post about a trip.

- **`commentary` — Commentary** *(see §6)*
  - Opinion / hot-take posts on politics, tech industry, society. Currently mixed into `being-human` and `responsible-ai`.
  - Absorbs: nothing existing (new) — splits commentary out of `being-human`
  - Example saved item: a quote-share of a political LinkedIn post with the author's framing.

---

## 4. Retire (do not migrate)

- **`powershell`** — last post 2011, sub-project of a sub-project. If the author ever returns to PowerShell, a fresh `software-craft` post with a `#powershell` tag handles it.
- **`projecteuler`** — same 7 posts as `powershell`, same retirement logic.
- **`python-series`** — 1 post, series never continued. Move the one post to `learning` with a `#python` tag and drop the category.

Total retired: 3 categories.

The 2011 PowerShell + ProjectEuler posts are not deleted, just uncategorised at the new schema level. They keep existing under their current URLs.

---

## 5. Migration map

| Old category | → | New category |
| --- | --- | --- |
| `being-human` | → | `being-human` (split: commentary-ish posts → `commentary`; family posts → `life`; rest stays) |
| `being-human-series` | → | `being-human` (series becomes `#bh-series` tag) |
| `agenticai` | → | `agentic-ai` (slug fix) |
| `intelligent-agents` | → | `intelligent-agents` |
| `ia-series` | → | `intelligent-agents` (series becomes `#ia-series` tag) |
| `vibe-engineering` | → | `vibe-engineering` (tool-heavy posts → `software-craft`) |
| `learning` | → | `learning` (paper-notes posts → `ml-research`; tool posts → `software-craft`) |
| `deep-learning` | → | `ml-research` |
| `research` | → | `ml-research` |
| `agi` | → | `ml-research` |
| `reinforcement-learning` | → | `ml-research` |
| `ml` | → | `ml-research` |
| `responsibleai` | → | `responsible-ai` (slug fix) |
| `python-series` | → | `learning` (+ `#python` tag) |
| `nn-series` | → | `learning` (+ `#nn-series` tag) |
| `rl-series` | → | `learning` (+ `#rl-series` tag) |
| `powershell` | → | *retire* |
| `projecteuler` | → | *retire* |

Net: 18 → 11. Series-as-category becomes series-as-tag (4 such moves).

---

## 6. New categories the saved-item patterns suggest

Justified from the LeStash saved-item shapes the design doc mentions and from gaps in the current set:

1. **`life`** — The "Sophie's work" use case explicitly named in the compose design has no current home. Posts like *Summer review, la rentrée est proche* or year-end reflections drift into `being-human` and dilute its philosophical character. A `life` category lets `being-human` stay reflective-philosophical and gives family/travel/personal content a clean place.

2. **`reading`** — The author has an Audible integration design (`docs/audible-integration-design.md`) and routinely cites books. Book-shaped content (highlights, citations, capsule reviews) is its own pattern — a saved highlight from Audible composes very differently from a paper note or a hot take. A dedicated category makes the compose template "book quote + framing" obvious.

3. **`software-craft`** — Posts like *Building Handy on Intel Mac*, *Domain Driven Design*, *Modern Python Package Management* are how-tos / tool-notes / design pattern revisits. They aren't LLM-flavoured so don't fit `vibe-engineering`, and they aren't paper notes so don't fit `ml-research`. Pulling them into one bucket sharpens both neighbours.

4. **`commentary`** — Political/industry hot takes (e.g. *How did America break itself?*, *Meta is now the pervy old man*) are currently in `being-human` or `responsible-ai`. They have a distinct rhythm: short, opinionated, often quoting another post. Worth its own bucket — and worth being explicit because the author can then decide to *not* surface this category in some shared contexts.

---

## 7. Open questions for the author

1. **Personal vs professional split — keep or merge?** This proposal keeps `life` separate from `being-human`. Alternative: one `personal` bucket that absorbs both. Lean on author taste — how openly do you want family content to mix with the AI / agency reflections that built `being-human`'s audience?
2. **`commentary` — keep or drop?** Cleanly splitting hot takes from `being-human` reads good on paper, but it risks sterilising `being-human` into "philosophy only." Is the bleed actually a feature?
3. **Granularity on AI sub-topics.** `ml-research` absorbs 5 existing categories (deep-learning, research, agi, RL, ML). Is that too coarse? Alternative: keep `reinforcement-learning` as its own top-level if it stays a recurring thread.
4. **`agentic-ai` vs `vibe-engineering` boundary.** Proposed split is third-person commentary (agentic-ai) vs first-person craft (vibe-engineering). Author may prefer the inverse framing or a merge.
5. **Series-as-tag — confirm.** This proposal demotes `*-series` categories to tags (`#bh-series`, `#ia-series`, etc.). That assumes tag autocomplete from the LeStash UX-compose design ships. If tags stay second-class on the blog, keeping series as categories is the lesser evil.
6. **Slugs — `agentic-ai` or `agentic`?** Two-word kebab is clearer in URLs and matches the human label; one word is shorter to type. Author taste.
7. **Migration cost.** Recategorising ~150+ posts manually is real work. Alternative: leave existing posts on existing categories, only enforce the new set for *future* posts via the LeStash composer. Old categories silently stop being suggested. The duplication tolerated as historical artefact.
