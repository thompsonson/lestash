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
lestash gmail doctor      # gws found, signed-in account, threads per search
lestash gmail backfill    # import everything that matches (first time)
```

After that, `lestash sources sync gmail` (and the sync timer) re-reads the last 30 days of each
search, which picks up new threads and late replies.

Settings live in `~/.config/lestash/gmail.json` (`queries`, optional `gws_path`). `gws` is found
on `PATH` or in the usual Homebrew/local locations, so it works from systemd services too.

## Items

| Item | `source_id` | Parent |
|------|-------------|--------|
| Thread (first message; list tag like `[UAI]` dropped from the title) | `gmail:thread:{threadId}` | — |
| Reply | `gmail:message:{messageId}` | the thread |

Google Groups footers are removed. The item link is the Google Groups page when the message has
one, otherwise the thread in Gmail.
