# lestash-jelu

[Jelu](https://github.com/bayang/jelu) source plugin for Le Stash. A one-way, read-only copy of
the books you track in Jelu, so they're searchable next to everything else. Jelu stays the
source of truth for what you've read.

## What gets imported

| Item | `source_id` | Parent |
|------|-------------|--------|
| Book (title, authors, status, tags, summary) | `jelu:book:{bookId}` | — |
| Your personal notes on the book | `jelu:notes:{userBookId}` | the book |
| Your review (rating in metadata) | `jelu:review:{reviewId}` | the book |
| A saved quote | `jelu:quote:{quoteId}` | the book |

Book metadata keeps `isbn13`, `isbn10` and `asin` (Jelu's Amazon id) for matching books across
sources (for example Audible).

## Setup

In Jelu, create an API token with the `books:read`, `reading:read`, `reviews:read` and
`lists:read` scopes, then:

```bash
lestash jelu auth --url https://your-jelu-host:8443   # prompts for the token
lestash jelu doctor                                   # counts books, reviews, notes, quotes
lestash sources sync jelu
```

The URL and token are saved to `~/.config/lestash/jelu.json` (mode 600).
