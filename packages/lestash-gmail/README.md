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

The first sync reads everything that matches. After that, each search reads mail since its
last successful read minus 2 days, so late replies are picked up and a stopped timer loses
nothing. A thread matched by several searches is read once per sync.

Failures stay visible but don't hold back the rest:

- A search that fails keeps its window; the other searches move on.
- A thread that can't be read is retried at the next syncs, and given up on after 5 reads.
- While anything fails, the sync is logged as failed, naming what failed.

`lestash gmail backfill` re-reads everything at any time and is logged like a sync; with
`--limit` it is logged `partial` and leaves the windows alone. `lestash gmail doctor` shows
where each search's next sync starts, and any threads waiting for a retry or given up.
The windows and retries are kept as a sync checkpoint.

Settings live in `~/.config/lestash/gmail.json` (`queries`, optional `gws_path`). `gws` is found
on `PATH` or in the usual Homebrew/local locations, so it works from systemd services too.

## Items

| Item | `source_id` | Parent |
|------|-------------|--------|
| Thread (first on-list message; list tag like `[UAI]` dropped from the titles) | `gmail:thread:{threadId}` | — |
| Reply | `gmail:message:{messageId}` | the thread |

For `list:` searches, only messages that went to the list are kept: list copies (`List-Id`) and
your own posts (the list is in To, Cc or Bcc). Private replies in a list thread are left out.
A thread's parent is its first list message, and it stays the same once stored. If a search
without `list:` also matches the thread, the whole thread is kept from then on, private
replies included. A thread remembers every search that matched it, so `remove-query` stops
new mail being read but doesn't un-keep messages already stored; delete those items to drop
them.
Reading `list:` out of complex boolean searches is best-effort: `-list:`, `NOT list:` and
negated groups are understood, but unusual forms may misparse.

A Google Groups footer at the end of a message is removed, and so are footers inside quoted
messages; text written below a quote is kept. Labels (read, starred, ...) aren't stored, since
they change. The item link is the Google Groups page when the message has
one, otherwise the thread in Gmail.
