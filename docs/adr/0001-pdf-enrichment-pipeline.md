# ADR 0001: PDF Enrichment Pipeline

*Status: Accepted — 2026-04-26*

## Context

Importing PDFs via Docling loses three classes of content that are present in the source:

1. **Hyperlinks** — exist only as PDF link annotations (`/Type /Annot /Subtype /Link`), invisible to any text extractor.
2. **Images** — replaced with `<!-- image -->` placeholders; the bytes are not retrieved.
3. **Ink annotations** — pen marks (Kobo eReader, tablet apps) ignored entirely.

Docling also injects unicode artifacts (`·` U+00B7, `◦` U+25E6) at list boundaries.

We need a pipeline that:
- recovers all three classes of content,
- can run over previously-imported items as well as new ones,
- is safe to re-run after extractor improvements.

This ADR is the authoritative decision record. Implementation details live in [`docs/pdf-enrichment-design.md`](../pdf-enrichment-design.md).

## Decisions

### D1: Library — Docling + PyMuPDF

Docling remains the primary extractor for its document-structure detection (headings, sections, tables, hierarchical markdown). PyMuPDF runs as a post-processing pass for the three gaps above.

**Rejected:** pdfplumber (MIT) handles links well but exposes only image bounding boxes, not bytes, and has no usable ink-annotation API. pypdf (BSD) needs manual link↔text association. No MIT/BSD library covers all three gaps in one pass.

### D2: License — AGPL-3.0

PyMuPDF is AGPL-3.0. Adopting it as a core dependency requires the project to relicense from MIT to AGPL-3.0. Acceptable: LeStash is already public, the copyright holder is the sole contributor, and the AGPL §13 source-disclosure obligation only affects third parties who deploy modified copies as a network service — not the primary single-user use case.

### D3: Pipeline shape — structured intermediate, not a string

The extractor returns a structured artifact (`EnrichedPdf`), not just markdown. The artifact carries the markdown plus the lists of extracted images, links, and classified annotations. The caller decides how to persist them. This keeps `text_extract.py` pure, makes the pipeline testable without the database, and lets backfill reuse the exact same code path.

### D4: Idempotency key — `(pdf_sha256, extractor_version)`

Every enrichment run is keyed on the SHA-256 of the source PDF bytes plus the integer extractor version. Stored on the item. Re-running the enricher on an item whose stored key matches the current key is a no-op. Bumping `extractor_version` invalidates all prior runs and causes the next `lestash enrich --all` to reprocess everything.

### D5: Source PDF retention — mandatory

The original PDF must be available for re-extraction. Two cases:

- **Drive-sourced**: store the Drive file ID and URL on an `item_media` row with `media_type='source_pdf'`. Re-extraction re-downloads from Drive. If the Drive file is gone, the item is marked unrunnable for that pass — never silently dropped.
- **Direct upload**: store the PDF bytes locally via the existing media storage at `~/.lestash/media/{item_id}/{hash}.pdf`, also as `media_type='source_pdf'`.

Backfill is meaningless without this — it is therefore a hard requirement, not a "nice to have".

### D6: Ink annotations — child items, one per *semantic* annotation

Each classified annotation (margin note, circled passage, underlined span, ink_unclassified) becomes a child item with `parent_id` set to the PDF item. Rationale:

- Reuses the existing parent-child pattern (LinkedIn reactions/comments).
- Searchable via FTS5 and embeddable for vector search out of the box.
- Default listings already filter `parent_id IS NULL`, so they don't clutter views.
- Each annotation can carry per-item metadata (page, bbox, color).

**Important refinement**: a child item is created per *semantic annotation*, not per ink stroke. A single circle is typically 5–20 strokes — those become one child. Stroke geometries are preserved in the child's `metadata` JSON for full-fidelity replay.

**Rejected:** JSON blob on `items.metadata` (annotations would not be searchable). Dedicated `item_annotations` table (additional join, no benefit over child items).

### D7: Invocation — single code path, two surfaces

The enricher is one function. It is called:

- **Inline** from `sync()` when a new PDF item is imported.
- **Standalone** via `lestash enrich [--item-id N | --all]` and `POST /api/items/{item_id}/enrich`.

Idempotency (D4) makes it safe to call from sync and re-run later.

### D8: Execution — synchronous

The enricher runs synchronously in the calling process. No job queue. A 50-page PDF taking 5–10s is acceptable for a personal-scale system. Revisit only if measured pain emerges.

### D9: Annotation classifier — geometric heuristics, with a safety valve

Classification of ink strokes uses pure geometric heuristics (no ML, no external deps):

- **Underline**: low y-variance, length above threshold.
- **Circle/ellipse**: closed loop (endpoint distance below threshold), reasonable aspect ratio.
- **Margin note**: bbox center in the outer X% of page width.
- **Stroke grouping**: cluster strokes by spatial+temporal proximity before classifying.

**Safety valve**: any annotation that does not match a known classifier falls through to `kind='ink_unclassified'` with raw geometry preserved. We never drop an annotation. Adding new classifiers in a later extractor version promotes existing `ink_unclassified` items on the next re-run — D4's versioning handles this cleanly.

### D10: Handwriting OCR — separate, opt-in pass via Claude multimodal vision

Handwritten text in margin notes and unclassified ink is not OCR'd locally. A separate enrichment pass sends the rendered annotation image to the **Claude API** (multimodal vision) on demand:

- Invoked via `lestash enrich --ocr [--item-id N | --all]`.
- Operates only on child items where `metadata.annotation_kind IN ('margin_note', 'ink_unclassified')`.
- Renders the stroke geometry to a PNG using PyMuPDF, sends to Claude with a fixed transcription prompt, writes the OCR'd text to the child item's `content` field.
- Idempotency keyed on `(stroke_geometry_hash, ocr_extractor_version)` — re-running does not re-spend.

**Why Claude vision, not Google Vision / Document AI / RapidOCR**: prior testing on real Kobo annotation samples showed RapidOCR scoring 0.50–0.70 on handwriting while Claude vision transcribed the same samples cleanly (e.g. "Add a title page", "reword this."). The `anthropic` SDK is already a project dependency, so this adds no new vendor surface area.

Kept separate from the main enricher because: it requires network + API credentials + per-call cost, the main enricher must stay offline-capable, and OCR is independently versionable from the geometric extractor.

## Consequences

**Positive**
- Hyperlinks, images, and ink annotations all recovered in one pipeline.
- Re-runnable over historical content with no special-case code.
- Annotations are first-class searchable items.
- Extractor improvements automatically reach old items via version bump + `lestash enrich --all`.

**Negative**
- AGPL-3.0 adoption is one-way: hard to switch back.
- Extractor version bumps will trigger full re-processing on the next `enrich --all` run — by design, but expensive on large libraries.
- Source PDF retention increases disk usage for direct-upload items (Drive items only store a reference).
- Heavily-annotated PDFs may produce dozens of child items per parent.

**Neutral**
- A new `docs/adr/` convention is established. Future load-bearing decisions go here.
