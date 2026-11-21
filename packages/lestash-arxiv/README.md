# lestash-arxiv

arXiv source plugin for Le Stash.

## Commands

```bash
lestash arxiv search "query"     # Search arXiv papers
lestash arxiv save <arxiv_id>    # Save paper to knowledge base
lestash arxiv info <arxiv_id>    # Show paper details
lestash arxiv merge-links [--apply]   # Merge saved links to arXiv papers into their arXiv items
lestash arxiv fetch-pdfs [--apply]    # Store and read the PDF of each arXiv item without one
```

## Shares

- A shared arXiv address becomes the paper's arXiv item, with its PDF (`POST /api/arxiv/import-paper`, #395).
- A shared arXiv PDF file does too, with the shared file as its source PDF (#398).
- `merge-links` moves links saved before that into the papers: notes, share date, links, collections and tags.
  - A link with a PDF file of its own is kept, linked to the paper with `alternate_of`.
  - Back up the database before `--apply`.
