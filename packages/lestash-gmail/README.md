# lestash-gmail

Gmail source plugin for Le Stash: stash the threads matching saved Gmail searches, such as a
mailing list (`list:uai-list.googlegroups.com`). Read-only.

Mail is read through the [Google Workspace CLI](https://github.com/googleworkspace/cli) (`gws`),
which keeps its own OAuth credentials, so LeStash never holds a Gmail token.

## Setup

```bash
# once, with read-only Gmail access
gws auth login --scopes https://www.googleapis.com/auth/gmail.readonly

lestash gmail add-query "list:uai-list.googlegroups.com"
lestash gmail doctor      # gws found, signed-in account, threads per search, last sync
lestash sources sync gmail
```

The first sync reads everything that matches. Each later sync reads mail since the last
successful Gmail sync (from the sync log) minus 2 days, so late replies are picked up and a
stopped timer loses nothing. If a thread can't be read, the others are still stored, the sync is
logged as failed, and the next sync re-reads the same window. `lestash gmail backfill` re-reads
everything at any time.

Settings live in `~/.config/lestash/gmail.json` (`queries`, optional `gws_path`). `gws` is found
on `PATH` or in the usual Homebrew/local locations, so it works from systemd services too.

## Items

| Item | `source_id` | Parent |
|------|-------------|--------|
| Thread (first on-list message; list tag like `[UAI]` dropped from the title) | `gmail:thread:{threadId}` | — |
| Reply | `gmail:message:{messageId}` | the thread |

For `list:` searches, only messages that went to the list are kept: list copies (`List-Id`) and
your own posts (the list is a recipient). Private replies in a list thread are left out.

A Google Groups footer at the end of a message is removed, and so are footers inside quoted
messages; text written below a quote is kept. Labels (read, starred, ...) aren't stored, since
they change. The item link is the Google Groups page when the message has
one, otherwise the thread in Gmail.
