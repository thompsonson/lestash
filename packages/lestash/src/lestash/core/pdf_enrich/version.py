"""Single source of truth for the PDF enrichment extractor version.

Bump when the extractor's behaviour changes in a way that should trigger
re-processing of previously enriched items via `lestash enrich --all`.

The OCR pass has its own version (see `ocr.py`) so it can evolve independently.
"""

EXTRACTOR_VERSION: int = 1
