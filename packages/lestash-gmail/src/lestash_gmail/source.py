"""Gmail source plugin: threads matching saved Gmail searches (e.g. a mailing list).

Each thread becomes a parent item (its first on-list message); replies are children.
Reads mail through `gws` with read-only access.
"""

import base64
import html
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Annotated, Any

import typer
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console

from lestash_gmail.client import GwsError, GwsGmail, find_gws, load_config, save_config

console = Console()
logger = get_plugin_logger("gmail")

# A sync re-reads mail since the last successful sync, minus this margin (late delivery,
# clock skew). With no successful sync yet, it reads the whole history of each search.
WINDOW_MARGIN = timedelta(days=2)

GROUPS_FOOTER = "You received this message because you are subscribed to the Google Groups"
LIST_FOOTER_WORDS = re.compile(r"unsubscribe|mailing list", re.I)
UNDERSCORE_RULE = re.compile(r"^_{20,}\s*$")
# A list footer after an underscore rule is short; a long tail is a quoted message (Outlook)
MAX_FOOTER_LINES = 6
GROUPS_LINK = re.compile(r"https://groups\.google\.com/d/msgid/[^\s>]+")
REPLY_PREFIX = re.compile(r"^(re|fwd?|aw|tr|sv|vs)\s*(\[\d+\])?\s*:\s*", re.I)
LEADING_TAG = re.compile(r"^\[([^\]]+)\]\s*")
# Tags that mail systems add in front of a subject, never part of it
MAILER_TAGS = frozenset({"external", "ext", "spam", "suspected spam", "caution", "warning"})
LIST_TERM = re.compile(r"\blist:\(?\"?([^\s\"()]+)", re.I)


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _find_part(part: dict[str, Any], mime: str) -> str | None:
    if part.get("mimeType") == mime and part.get("body", {}).get("data"):
        return _decode(part["body"]["data"])
    for child in part.get("parts") or []:
        found = _find_part(child, mime)
        if found:
            return found
    return None


def _is_quoted(line: str) -> bool:
    return line.lstrip().startswith(">")


def _strip_trailing_footer(lines: list[str]) -> list[str]:
    """Cut a list footer at the end of the message (unquoted lines only).

    A footer starts at the Google Groups sentence (or a "-- " line just before it), or at an
    underscore rule followed by a few lines that mention unsubscribing. Everything after the
    cut must be unquoted, so text below a quoted message (bottom-posting) is never touched.
    """
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    for i in range(end - 1, -1, -1):
        line = lines[i]
        if _is_quoted(line):
            break  # the footer is only ever after the last quoted line
        if GROUPS_FOOTER in line:
            cut = i
            if cut and lines[cut - 1].strip() in ("--", "-- "):
                cut -= 1
            return lines[:cut]
        if UNDERSCORE_RULE.match(line):
            tail = [t for t in lines[i + 1 : end] if t.strip()]
            if len(tail) <= MAX_FOOTER_LINES and any(LIST_FOOTER_WORDS.search(t) for t in tail):
                return lines[:i]
            break
    return lines


def _strip_quoted_footers(lines: list[str]) -> list[str]:
    """Drop Google Groups footers inside quoted messages ("> -- " ... end of that quote).

    A quoted footer runs to the end of its quoted block; the reply around it is kept.
    """
    out: list[str] = []
    skipping = False
    for i, line in enumerate(lines):
        if skipping:
            if _is_quoted(line):
                continue
            skipping = False
        if _is_quoted(line):
            text = line.lstrip().lstrip(">").strip()
            nxt = lines[i + 1].lstrip().lstrip(">") if i + 1 < len(lines) else ""
            if GROUPS_FOOTER in text or (text in ("--", "") and GROUPS_FOOTER in nxt):
                # Also drop an empty quoted line just before the separator
                while out and _is_quoted(out[-1]) and not out[-1].lstrip().lstrip(">").strip():
                    out.pop()
                skipping = True
                continue
        out.append(line)
    return out


def message_text(message: dict[str, Any]) -> str:
    """Plain-text body (HTML stripped if there is no text part), without list footers."""
    payload = message.get("payload", {})
    text = _find_part(payload, "text/plain")
    if text is None:
        raw = _find_part(payload, "text/html") or ""
        raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
        raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", raw)
        text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    lines = text.replace("\r\n", "\n").split("\n")
    lines = _strip_quoted_footers(_strip_trailing_footer(lines))
    while lines and not lines[-1].strip().lstrip(">").strip():
        lines.pop()  # empty quote lines left at the end
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _message_date(message: dict[str, Any], headers: dict[str, str]) -> datetime | None:
    if message.get("internalDate"):
        return datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC)
    try:
        return parsedate_to_datetime(headers.get("date", ""))
    except (TypeError, ValueError):
        return None


def clean_subject(subject: str, list_tags: frozenset[str] | None = None) -> str:
    """Drop reply prefixes ("Re:", "Fwd:"), the list tag ("[UAI]") and mailer tags.

    They are removed in any order and any number of times. Other leading tags such as
    "[CFP]" are part of the subject and are kept. With ``list_tags=None`` the first
    leading tag is taken to be the list's; pass an empty set to keep every tag.
    """
    text = (subject or "").strip()
    known = {t.lower() for t in list_tags or ()}
    guess_list_tag = list_tags is None
    while True:
        if match := REPLY_PREFIX.match(text):
            text = text[match.end() :]
            continue
        if match := LEADING_TAG.match(text):
            tag = match.group(1).strip().lower()
            rest = text[match.end() :]
            if tag in known or tag in MAILER_TAGS:
                text = rest
                continue
            if guess_list_tag or REPLY_PREFIX.match(rest):
                # The first other tag is the list's; a tag before "Re:" was added in front
                guess_list_tag = False
                known.add(tag)
                text = rest
                continue
        break
    return text.strip() or "(no subject)"


def _leading_tags(subject: str) -> list[str]:
    tags = []
    text = (subject or "").strip()
    while True:
        if match := REPLY_PREFIX.match(text):
            text = text[match.end() :]
        elif match := LEADING_TAG.match(text):
            tags.append(match.group(1).strip())
            text = text[match.end() :]
        else:
            return tags


def _norm_list(address: str) -> str:
    """'uai-list@googlegroups.com', '<uai-list.googlegroups.com>' -> 'uai-list.googlegroups.com'."""
    return address.strip().strip("<>").strip().lower().replace("@", ".")


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _list_names(message: dict[str, Any], query: str) -> list[str]:
    """Names a list goes by: List-Id's address and display name, and the search's `list:`."""
    list_id = _headers(message).get("list-id", "")
    names = [_norm_list(term).split(".")[0] for term in list_terms(query)]
    if inner := re.search(r"<([^>]+)>", list_id):
        names += [_norm_list(inner.group(1)).split(".")[0], list_id[: inner.start()]]
    elif list_id:
        names.append(_norm_list(list_id).split(".")[0])
    return [n for n in map(_alnum, names) if n]


def root_list_tag(message: dict[str, Any], query: str) -> frozenset[str]:
    """The root's first tag, if it names the list ("[UAI]" for uai-list); else none.

    A list without a subject tag keeps a real one: "[UAI 2026] Deadline" stays whole.
    """
    tags = [
        t
        for t in _leading_tags(_headers(message).get("subject", ""))
        if t.lower() not in MAILER_TAGS
    ]
    if not tags or not (tag := _alnum(tags[0])):
        return frozenset()
    names = _list_names(message, query)
    return frozenset(tags[:1]) if any(tag in name for name in names) else frozenset()


def list_terms(query: str) -> frozenset[str]:
    """Mailing lists named by `list:` in a Gmail search, normalised."""
    return frozenset(_norm_list(t) for t in LIST_TERM.findall(query))


def is_on_list(message: dict[str, Any], lists: frozenset[str]) -> bool:
    """Whether a message went to one of the lists (always True if the search names none).

    List copies carry a List-Id header. Your own posts are kept from your Sent copy, which
    has no List-Id, by checking whether the list was a recipient. Private replies inside a
    list thread (To/Cc without the list) are not on the list.
    """
    if not lists:
        return True
    headers = _headers(message)
    list_id = headers.get("list-id", "")
    if list_id:
        inner = re.search(r"<([^>]+)>", list_id)
        if _norm_list(inner.group(1) if inner else list_id) in lists:
            return True
    recipients = getaddresses([headers.get("to", ""), headers.get("cc", "")])
    return any(_norm_list(addr) in lists for _, addr in recipients if addr)


def message_to_item(
    message: dict[str, Any],
    query: str,
    *,
    parent_source_id: str | None = None,
    list_tags: frozenset[str] | None = None,
) -> ItemCreate:
    headers = _headers(message)
    raw_text = message_text(message)
    groups_link = GROUPS_LINK.search(_find_part(message.get("payload", {}), "text/plain") or "")
    name, address = parseaddr(headers.get("from", ""))
    subject = headers.get("subject", "")
    is_reply = parent_source_id is not None

    # Labels (read, starred, ...) change all the time, so they aren't stored.
    metadata: dict[str, Any] = {
        "type": "reply" if is_reply else "thread",
        "query": query,
        "thread_id": message.get("threadId"),
        "message_id": headers.get("message-id"),
        "from_email": address,
        "list_id": headers.get("list-id"),
        "subject": subject,
    }
    if groups_link:
        metadata["groups_url"] = groups_link.group(0)
    if parent_source_id:
        metadata["_parent_source_id"] = parent_source_id

    thread_id = message.get("threadId", "")
    return ItemCreate(
        source_type="gmail",
        source_id=f"gmail:message:{message['id']}" if is_reply else f"gmail:thread:{thread_id}",
        url=groups_link.group(0)
        if groups_link
        else f"https://mail.google.com/mail/u/0/#all/{thread_id}",
        title=("Re: " if is_reply else "") + clean_subject(subject, list_tags),
        content=raw_text or subject,
        author=name or address or None,
        created_at=_message_date(message, headers),
        is_own_content=False,
        metadata={k: v for k, v in metadata.items() if v not in (None, "", [])},
    )


def thread_to_items(thread: dict[str, Any], query: str) -> list[ItemCreate]:
    """First on-list message as the parent, the other on-list messages as its children.

    Off-list messages in the thread (private replies) are left out; see is_on_list.
    """
    lists = list_terms(query)
    messages = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))
    messages = [m for m in messages if is_on_list(m, lists)]
    if not messages:
        return []
    list_tags = root_list_tag(messages[0], query)
    parent = message_to_item(messages[0], query, list_tags=list_tags)
    children = [
        message_to_item(m, query, parent_source_id=parent.source_id, list_tags=list_tags)
        for m in messages[1:]
    ]
    return [parent, *children]


def search_query(query: str, since: datetime | None) -> str:
    """The Gmail search for one sync: everything, or mail after `since` minus a margin."""
    if since is None:
        return query
    if since.tzinfo is None:
        since = since.astimezone()  # sync_log stores local time
    return f"({query}) after:{int((since - WINDOW_MARGIN).timestamp())}"


def sync_queries(
    gmail: GwsGmail,
    queries: list[str],
    since: datetime | None = None,
    limit: int | None = None,
) -> Iterator[ItemCreate]:
    """Items for every thread matching each search (since `since`, if given).

    A search or thread that fails is skipped and the rest are still yielded; then GwsError
    is raised, so the sync is logged as failed and the next one re-reads the same window.
    """
    failed: list[str] = []
    skipped = 0
    for query in queries:
        try:
            for thread_id in gmail.thread_ids(search_query(query, since), limit=limit):
                try:
                    thread = gmail.thread(thread_id)
                except GwsError as e:
                    skipped += 1
                    logger.warning(f"Skipping thread {thread_id}: {e}")
                    continue
                yield from thread_to_items(thread, query)
        except GwsError as e:
            failed.append(query)
            logger.warning(f"Search {query!r} failed: {e}")
    problems = [f"{skipped} thread(s) could not be read"] if skipped else []
    if failed:
        problems.append(f"{len(failed)} search(es) failed: {', '.join(failed)}")
    if problems:
        raise GwsError("; ".join(problems) + "; they are retried next sync")


def last_successful_sync() -> datetime | None:
    """Start time of the last completed Gmail sync, from the sync log."""
    from lestash.core.config import Config
    from lestash.core.database import get_connection

    with get_connection(Config.load()) as conn:
        row = conn.execute(
            "SELECT MAX(started_at) FROM sync_log WHERE source_type = 'gmail' "
            "AND status = 'completed'"
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(str(row[0]))
    except ValueError:
        return None


def run_backfill(
    conn: Any, gmail: GwsGmail, queries: list[str], limit: int | None = None
) -> tuple[int, str | None]:
    """Import every matching thread and record the run in the sync log, like a sync.

    The run is logged ``completed`` only if every thread was read and no limit was set, so
    the next sync starts its window from it. Otherwise it is logged ``failed`` (a thread
    was skipped) or ``partial`` (``--limit``), and the next sync still reads everything
    after the last complete run.

    Returns:
        (messages imported, error message or None).
    """
    from lestash.core.database import resolve_parent_id, resolve_parent_source_ids, upsert_item

    started_at = datetime.now()
    sync_id = conn.execute(
        "INSERT INTO sync_log (source_type, started_at, status) VALUES (?, ?, ?)",
        ("gmail", started_at.isoformat(" "), "running"),
    ).lastrowid
    conn.commit()

    count = 0
    error: str | None = None
    status = "completed"
    try:
        for item in sync_queries(gmail, queries, limit=limit):
            resolve_parent_id(conn, item)
            upsert_item(conn, item)
            count += 1
        resolve_parent_source_ids(conn, "gmail")
        if limit:
            status = "partial"
            error = f"backfill limited to {limit} threads per search"
    except GwsError as e:
        status, error = "failed", str(e)
        resolve_parent_source_ids(conn, "gmail")
    conn.execute(
        """
        UPDATE sync_log SET completed_at = ?, status = ?, items_added = ?, error_message = ?
        WHERE id = ?
        """,
        (datetime.now().isoformat(" "), status, count, error, sync_id),
    )
    conn.commit()
    return count, error


class GmailSource(SourcePlugin):
    """Gmail source plugin (via the gws CLI)."""

    name = "gmail"
    description = "Gmail threads from saved searches (via gws)"

    def get_commands(self) -> typer.Typer:
        app = typer.Typer(help="Gmail commands (read-only, via the gws CLI).")

        @app.command("add-query")
        def add_query(
            query: Annotated[
                str, typer.Argument(help='Gmail search, e.g. "list:uai-list.googlegroups.com"')
            ],
        ) -> None:
            """Add a Gmail search to sync."""
            config = load_config()
            if query not in config["queries"]:
                config["queries"].append(query)
                save_config(config)
            console.print(f"Queries: {config['queries']}")

        @app.command("remove-query")
        def remove_query(query: Annotated[str, typer.Argument()]) -> None:
            """Stop syncing a Gmail search (items already stashed are kept)."""
            config = load_config()
            config["queries"] = [q for q in config["queries"] if q != query]
            save_config(config)
            console.print(f"Queries: {config['queries']}")

        @app.command("doctor")
        def doctor() -> None:
            """Check gws, the Gmail sign-in and how many threads each search matches."""
            config = load_config()
            gws = find_gws(config.get("gws_path"))
            if not gws:
                console.print(
                    "[red]gws not found. Install it, or set gws_path in gmail.json.[/red]"
                )
                raise typer.Exit(1)
            gmail = GwsGmail(gws)
            try:
                console.print(f"gws: {gws} — signed in as {gmail.profile().get('emailAddress')}")
                for query in config["queries"]:
                    count = sum(1 for _ in gmail.thread_ids(query))
                    console.print(f"  {query}: {count} threads")
            except GwsError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1) from e
            since = last_successful_sync()
            console.print(f"Last successful sync: {since or 'never (next sync reads everything)'}")
            if not config["queries"]:
                console.print("No queries yet: lestash gmail add-query 'list:...'")

        @app.command("backfill")
        def backfill(
            limit: Annotated[
                int | None, typer.Option(help="Stop after this many threads per query")
            ] = None,
        ) -> None:
            """Import all matching threads, whatever the last sync was."""
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            config = load_config()
            gws = find_gws(config.get("gws_path"))
            if not gws:
                console.print("[red]gws not found.[/red]")
                raise typer.Exit(1)
            with get_connection(Config.load()) as conn:
                count, error = run_backfill(conn, GwsGmail(gws), config["queries"], limit=limit)
            if error:
                console.print(f"[yellow]{error}[/yellow]")
            console.print(f"[green]Imported {count} messages.[/green]")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Threads with mail since the last successful sync (all of it the first time)."""
        settings = load_config()
        if not settings["queries"]:
            logger.warning("No Gmail queries configured; run `lestash gmail add-query`")
            return
        gws = find_gws(settings.get("gws_path"))
        if not gws:
            raise GwsError("gws not found; install it or set gws_path in gmail.json")
        yield from sync_queries(GwsGmail(gws), settings["queries"], since=last_successful_sync())

    def configure(self) -> dict:
        return {}
