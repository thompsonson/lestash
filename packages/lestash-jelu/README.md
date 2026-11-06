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
sources. Audible audiobooks usually have their own ASIN and ISBN, so matching them to Jelu books
will mostly need title and author.

Reviews or quotes for a book that isn't in your library get no parent; the Jelu book id is kept
in `metadata.jelu_book_id`.

## Setup

```bash
lestash jelu auth --url https://your-jelu-host:8443 -u <login>   # prompts for the password
lestash jelu doctor                                               # counts books, reviews, notes, quotes
lestash sources sync jelu
```

## How it signs in

The plugin signs in the way Jelu's web UI does: `GET /api/v1/users/me` with your username and
password (HTTP Basic) returns a session token, and every later request sends it as
`X-Auth-Token`. The token is reused across syncs; if Jelu has dropped the session (after a week
without use, by default), the plugin signs in again.

Jelu's scoped API tokens are not used. Jelu only lets them reach the paths in its scope map, and
`/users/me` and `/book-quotes` are not in it, so a token can't read your user id or your quotes.

The URL, username, password and current session token are saved to
`~/.config/lestash/jelu.json` with mode 600. Storing the password is what lets the timer sync keep
working after a session expires. The alternative, saving only the session token, would mean
running `auth` again whenever the session lapses (for example after a week with pop-mini off).

If the plugin isn't configured, `lestash sources sync` logs a warning and syncs nothing (like the
other plugins). Run `lestash sources disable jelu` to silence it. A saved login that Jelu rejects
fails the sync.
