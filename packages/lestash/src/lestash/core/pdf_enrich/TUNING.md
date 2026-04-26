# PDF Enrichment — Tuning Notes

The annotation classifier in `annotations.py` uses pure geometric heuristics
with constants chosen as sensible defaults. They have **not** been verified
against a corpus of real Kobo / iPad Notes / xournal++ output. Expect to
adjust them as soon as you have real samples.

## Constants and what to look at

| Constant | Default | What goes wrong if it's wrong |
|---|---|---|
| `UNDERLINE_Y_RATIO_MAX` | `0.10` | Too low: real Kobo underlines (often slightly slanted) classify as `ink_unclassified`. Too high: short scribbles look like underlines. |
| `UNDERLINE_MIN_LENGTH_PT` | `20.0` | Too low: small dashes get tagged underline. Too high: short underlines for single words are missed. |
| `CIRCLE_CLOSURE_RATIO_MAX` | `0.20` | Too low: hand-drawn open ovals miss. Too high: long curves get mis-tagged as circles. |
| `CIRCLE_MIN_POINTS` | `12` | Too low: short squiggles look like circles. Too high: small circled words are missed. |
| `MARGIN_OUTER_FRACTION` | `0.18` | Too low: notes near body text get tagged margin_note. Too high: real margin notes get tagged ink_unclassified. |
| `STROKE_GROUPING_DISTANCE_PT` | `30.0` | Too low: a single circle stays as 8 separate annotations. Too high: distinct annotations on the same page merge. |

## How to tune

1. Take a real Kobo PDF (or whatever your dominant source is) with a known
   set of annotations.
2. Run `lestash enrich --item-id N`.
3. Inspect `lestash items <child_id>` for each child created — confirm `kind`
   matches your intent.
4. Adjust the constant whose threshold the failing case is just outside.
5. Bump `EXTRACTOR_VERSION` in `version.py`.
6. Run `lestash enrich --all` — tuning improvements propagate to existing items.

## Known limitations

- **Strikethrough vs. underline** — both are roughly horizontal; we don't
  distinguish them. Future work would compare the stroke's y-center to the
  text's vertical midline.
- **Highlighter pen strokes** — these often arrive as `Ink` annotations in
  pen apps but as `Highlight` annotations from PDF readers. We only handle
  `Ink` today.
- **Handwritten margin notes** — text content is not OCR'd by the geometric
  classifier. Use `lestash enrich --ocr` (Claude vision) to add transcribed
  `content`.
