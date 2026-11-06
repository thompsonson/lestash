"""Gmail source plugin: threads matching saved Gmail searches (e.g. a mailing list).

Each thread becomes a parent item (its first on-list message); replies are children.
Reads mail through `gws` with read-only access.
"""

import base64
import html
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from functools import partial
from typing import Annotated, Any

import typer
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.models.sync import SyncCheckpoint
from lestash.plugins.base import SourcePlugin
from rich.console import Console

from lestash_gmail.client import GwsError, GwsGmail, find_gws, load_config, save_config

console = Console()
logger = get_plugin_logger("gmail")

# Each search re-reads mail since its last successful read, minus this margin (late
# delivery, clock skew). A search never read yet reads its whole history.
WINDOW_MARGIN = timedelta(days=2)
# Sync state, kept as one checkpoint: {"windows": {search: iso time}, "retry": {...},
# "given_up": [thread ids]}
STATE_KEY = "state"
# A thread that can't be read is retried at the next syncs, then given up on
MAX_THREAD_ATTEMPTS = 5

GROUPS_FOOTER = "You received this message because you are subscribed to the Google Groups"
LIST_FOOTER_WORDS = re.compile(r"unsubscribe|mailing list", re.I)
UNDERSCORE_RULE = re.compile(r"^_{20,}\s*$")
# A list footer after an underscore rule is short; a long tail is a quoted message (Outlook)
MAX_FOOTER_LINES = 6
# The lines of a Google Groups footer, from its first sentence on
GROUPS_FOOTER_LINES = re.compile(
    r"^(You received this message because you are subscribed to the Google Groups"
    r"|To unsubscribe from this group|To view this discussion|To post to this group"
    r"|Visit this group at|For more options|https://groups\.google\.com/)"
)
# An underscore rule followed by these is an Outlook quote ("From: Bob"), not a footer
OUTLOOK_HEADER = re.compile(r"^(From|De|Von|Da|Van|Från|Fra):\s", re.I)
QUOTE_MARKS = re.compile(r"^[\s>]*")
GROUPS_LINK = re.compile(r"https://groups\.google\.com/d/msgid/[^\s>]+")
REPLY_PREFIX = re.compile(r"^(re|fwd?|aw|tr|sv|vs)\s*(\[\d+\])?\s*:\s*", re.I)
LEADING_TAG = re.compile(r"^\[([^\]]+)\]\s*")
# Tags that mail systems add in front of a subject, never part of it
MAILER_TAGS = frozenset({"external", "ext", "spam", "suspected spam", "caution", "warning"})
# `list:x`, `list:"x"`, `list:(a OR b)`, `list:{a b}`; not `-list:x`, which excludes x
NEGATED_GROUP = re.compile(r"(?<![\w-])-(\([^)]*\)|\{[^}]*\})")
NOT_BEFORE = re.compile(r"\bNOT\s+$")
LIST_TERM = re.compile(r'(?<![\w-])list:(\([^)]*\)|\{[^}]*\}|"[^"]*"|[^\s()]+)', re.I)


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


def _depth(line: str) -> int:
    """How deeply a line is quoted: "> > x" is 2."""
    marks = QUOTE_MARKS.match(line)
    return marks.group(0).count(">") if marks else 0


def _unquote(line: str) -> str:
    """The line without its quote marks and leading spaces: "> > -- " gives "-- "."""
    marks = QUOTE_MARKS.match(line)
    return line[marks.end() :] if marks else line


def _is_quoted(line: str) -> bool:
    return _depth(line) > 0


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
            if tail and OUTLOOK_HEADER.match(tail[0].strip()):
                break  # a quoted message (Outlook), however short
            if len(tail) <= MAX_FOOTER_LINES and any(LIST_FOOTER_WORDS.search(t) for t in tail):
                return lines[:i]
            break
    return lines


def _footer_starts(lines: list[str], i: int) -> bool:
    """Whether a Google Groups footer starts at line i: its sentence, or "-- " just before.

    Inside a quote the sentence may be anywhere on the line. Outside a quote (an Outlook
    quote has no ">") the line must start with it, so text that mentions it is kept.
    """
    depth = _depth(lines[i])

    def is_sentence(text: str) -> bool:
        return GROUPS_FOOTER in text if depth else text.startswith(GROUPS_FOOTER)

    text = _unquote(lines[i]).strip()
    if is_sentence(text):
        return True
    if i + 1 >= len(lines) or _depth(lines[i + 1]) != depth:
        return False
    separators = ("--", "") if depth else ("--",)
    return text in separators and is_sentence(_unquote(lines[i + 1]).strip())


def _strip_inner_footers(lines: list[str]) -> list[str]:
    """Drop Google Groups footers inside the message, i.e. in quoted messages.

    A footer in a ">" quote, at any depth, runs to the end of its quote (the lines quoted
    as deeply or more); the reply around it is kept. A footer in a quote without ">"
    (Outlook) is only its own lines: the "-- " line, the Groups sentence, and the Groups
    lines after it ("To unsubscribe from this group..."), so no written text is lost.
    """
    out: list[str] = []
    skip: int | None = None  # quote depth of the footer being dropped
    for i, line in enumerate(lines):
        depth = _depth(line)
        if skip is not None:
            if depth >= skip and (skip or GROUPS_FOOTER_LINES.match(_unquote(line).strip())):
                continue
            skip = None
        if _footer_starts(lines, i):
            # Also drop empty lines just before the footer
            while out and _depth(out[-1]) >= depth and not _unquote(out[-1]).strip():
                out.pop()
            skip = depth
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
    lines = _strip_inner_footers(_strip_trailing_footer(lines))
    while lines and not _unquote(lines[-1]).strip():
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


def _names_list(tag: str, message: dict[str, Any], queries: Sequence[str]) -> bool:
    """Whether a subject tag names the message's list ("[UAI]" for uai-list).

    The tag must equal a leading run of the words in a list address ("uai", "uailist" for
    uai-list), from List-Id or the searches' `list:`, or the whole List-Id display name.
    So "[AI]" and "[List]" don't name uai-list.
    """
    tag = _alnum(tag)
    if not tag:
        return False
    list_id = _headers(message).get("list-id", "")
    inner = re.search(r"<([^>]+)>", list_id)
    addresses = [term for q in queries for term in list_terms(q)]
    addresses.append(inner.group(1) if inner else list_id)
    for address in addresses:
        words = [w for w in re.split(r"[^a-z0-9]+", _norm_list(address).split(".")[0]) if w]
        if tag in {"".join(words[: i + 1]) for i in range(len(words))}:
            return True
    return inner is not None and tag == _alnum(list_id[: inner.start()])


def thread_list_tags(messages: list[dict[str, Any]], queries: Sequence[str]) -> frozenset[str]:
    """The list's subject tags in a thread ("[UAI]"): first tags that name the list.

    Each message's first tag counts, not only the root's: your own Sent copy as the root
    has no tag (the list adds it on delivery), but the replies do. A list without a
    subject tag keeps a real one: "[UAI 2026] Deadline" stays whole.
    """
    found = set()
    for message in messages:
        tags = [
            t
            for t in _leading_tags(_headers(message).get("subject", ""))
            if t.lower() not in MAILER_TAGS
        ]
        if tags and _names_list(tags[0], message, queries):
            found.add(tags[0])
    return frozenset(found)


def list_terms(query: str) -> frozenset[str]:
    """Mailing lists a Gmail search keeps with `list:`, normalised (negations left out).

    Best effort: `-list:x`, `NOT list:x`, `list:(a -b)` and `list:` inside a `-(...)` or
    `-{...}` group are negations. Other boolean searches may misparse.
    """
    negated = [m.span() for m in NEGATED_GROUP.finditer(query)]
    terms = set()
    for match in LIST_TERM.finditer(query):
        start = match.start()
        if any(a <= start < b for a, b in negated) or NOT_BEFORE.search(query[:start]):
            continue
        for word in re.split(r'[\s(){}"]+', match.group(1)):
            if word and word.upper() not in ("OR", "AND") and not word.startswith("-"):
                terms.add(_norm_list(word))
    return frozenset(terms)


def is_on_list(message: dict[str, Any], lists: frozenset[str]) -> bool:
    """Whether a message went to one of the lists (always True if the search names none).

    List copies carry a List-Id header. Your own posts are kept from your Sent copy, which
    has no List-Id, by checking whether the list was a recipient (To, Cc or Bcc). Private
    replies inside a list thread (no list among the recipients) are not on the list.
    """
    if not lists:
        return True
    headers = _headers(message)
    list_id = headers.get("list-id", "")
    if list_id:
        inner = re.search(r"<([^>]+)>", list_id)
        if _norm_list(inner.group(1) if inner else list_id) in lists:
            return True
    # Only headers that are there: getaddresses gives no address at all for two empty ones
    recipients = getaddresses([headers[h] for h in ("to", "cc", "bcc") if headers.get(h)])
    return any(_norm_list(addr) in lists for _, addr in recipients if addr)


def message_to_item(
    message: dict[str, Any],
    queries: str | Sequence[str],
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
        # Sorted, so a thread matched by several searches is stored the same way each sync
        "queries": sorted(_as_list(queries)),
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


def _as_list(queries: str | Sequence[str]) -> list[str]:
    return [queries] if isinstance(queries, str) else list(queries)


def thread_to_items(
    thread: dict[str, Any],
    queries: str | Sequence[str],
    stored: Mapping[str, Any] | None = None,
) -> list[ItemCreate]:
    """The thread as a parent item, with the other kept messages as its children.

    ``queries`` are the searches that matched the thread now; ``stored`` is what the
    thread's stored item recorded (see stored_threads). Its searches are added to them,
    so what is kept only grows: once a search without `list:` matched, the whole thread
    stays kept. A message is kept if any of the searches keeps it: a search without
    `list:` keeps the whole thread, `list:` searches keep the messages that went to their
    lists; see is_on_list.

    The parent is the stored one, if any. Otherwise it is the first message that went to
    the lists (the first message, for searches without `list:`), so an off-list message
    before it is a child. The parent then never changes as the matching searches change.
    """
    stored = stored or {}
    queries = sorted({*_as_list(queries), *stored.get("queries", [])})
    keep_all = any(not list_terms(q) for q in queries)
    lists = frozenset().union(*(list_terms(q) for q in queries))
    messages = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))
    messages = [m for m in messages if keep_all or is_on_list(m, lists)]
    if not messages:
        return []
    root_id = stored.get("message_id")
    root = next((m for m in messages if root_id and _headers(m).get("message-id") == root_id), None)
    if root is None:
        root = next((m for m in messages if is_on_list(m, lists)), messages[0])
    list_tags = thread_list_tags(messages, queries)
    parent = message_to_item(root, queries, list_tags=list_tags)
    children = [
        message_to_item(m, queries, parent_source_id=parent.source_id, list_tags=list_tags)
        for m in messages
        if m is not root
    ]
    return [parent, *children]


def stored_threads(conn: Any, thread_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """What the stored thread items recorded, by thread id: their searches and root.

    One query per 500 threads. Items stored before ``queries`` existed have ``query``.
    """
    found: dict[str, dict[str, Any]] = {}
    source_ids = [f"gmail:thread:{t}" for t in thread_ids]
    for start in range(0, len(source_ids), 500):
        chunk = source_ids[start : start + 500]
        rows = conn.execute(
            "SELECT source_id, metadata FROM items WHERE source_type = 'gmail' "
            f"AND source_id IN ({', '.join('?' * len(chunk))})",
            chunk,
        ).fetchall()
        for source_id, raw in rows:
            try:
                metadata = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                metadata = {}
            searches = metadata.get("queries") or (
                [metadata["query"]] if metadata.get("query") else []
            )
            found[source_id.removeprefix("gmail:thread:")] = {
                "queries": searches,
                "message_id": metadata.get("message_id"),
            }
    return found


def load_stored_threads(thread_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """stored_threads, on a connection of its own (for a sync run by the CLI or server)."""
    from lestash.core.config import Config
    from lestash.core.database import get_connection

    with get_connection(Config.load()) as conn:
        return stored_threads(conn, thread_ids)


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
    since: Mapping[str, datetime | None] | None = None,
    limit: int | None = None,
    state: Mapping[str, Any] | None = None,
    stored: Callable[[Sequence[str]], Mapping[str, Mapping[str, Any]]] | None = None,
) -> Iterator[ItemCreate | SyncCheckpoint]:
    """Items for every thread matching the searches, then the new sync state.

    ``since`` gives each search's window start (see search_windows); a search not in it
    reads everything. ``state`` is the previous sync state, for its failed threads.

    Each thread is read once, even when several searches match it, and its items record
    every search that matched (see thread_to_items). ``stored`` looks up what the stored
    thread items recorded, once for all threads (see stored_threads).

    A failed search keeps its old window; the others move on to the start of this sync. A
    thread that can't be read is retried at the next syncs, up to MAX_THREAD_ATTEMPTS
    reads. The state is yielded as a SyncCheckpoint (not with ``limit``, which reads only
    part of each window). Then, if anything failed, GwsError is raised, so the sync is
    logged as failed.
    """
    started = datetime.now(UTC)
    since = since or {}
    old_windows: dict[str, str] = dict((state or {}).get("windows", {}))
    old_retry: dict[str, dict[str, Any]] = dict((state or {}).get("retry", {}))
    given_up_before: list[str] = list((state or {}).get("given_up", []))

    matched: dict[str, list[str]] = {}  # thread id -> searches that matched it, in order
    failed: list[str] = []
    for query in queries:
        try:
            for thread_id in gmail.thread_ids(search_query(query, since.get(query)), limit=limit):
                matched.setdefault(thread_id, []).append(query)
        except GwsError as e:
            failed.append(query)
            logger.warning(f"Search {query!r} failed: {e}")
    for thread_id, entry in old_retry.items():
        retry_queries = [q for q in entry.get("queries", []) if q in queries]
        if retry_queries and thread_id not in matched:
            matched[thread_id] = retry_queries

    known = stored(list(matched)) if stored and matched else {}
    retry: dict[str, dict[str, Any]] = {}
    given_up: list[str] = []
    read: set[str] = set()
    for thread_id, thread_queries in matched.items():
        try:
            thread = gmail.thread(thread_id)
        except GwsError as e:
            attempts = old_retry.get(thread_id, {}).get("attempts", 0) + 1
            if attempts >= MAX_THREAD_ATTEMPTS:
                given_up.append(thread_id)
                logger.warning(f"Giving up on thread {thread_id} after {attempts} reads: {e}")
            else:
                retry[thread_id] = {"queries": thread_queries, "attempts": attempts}
                logger.warning(f"Skipping thread {thread_id} (retried next sync): {e}")
            continue
        read.add(thread_id)
        yield from thread_to_items(thread, thread_queries, known.get(thread_id))

    if not limit:
        windows = {q: started.isoformat() for q in queries if q not in failed}
        for q in failed:  # keep the window it had, saved or from the sync log
            if q in old_windows:
                windows[q] = old_windows[q]
            elif start := since.get(q):
                windows[q] = start.isoformat()
        # Given-up threads are listed (by `doctor`) until one is read again
        gave_up = [t for t in dict.fromkeys(given_up_before + given_up) if t not in read]
        yield SyncCheckpoint(
            key=STATE_KEY, value={"windows": windows, "retry": retry, "given_up": gave_up}
        )

    problems = []
    if retry:
        problems.append(f"{len(retry)} thread(s) could not be read; retried next sync")
    if given_up:
        problems.append(
            f"gave up on {len(given_up)} thread(s) after {MAX_THREAD_ATTEMPTS} reads: "
            + ", ".join(given_up)
        )
    if failed:
        problems.append(
            f"{len(failed)} search(es) failed and keep their window: {', '.join(failed)}"
        )
    if problems:
        raise GwsError("; ".join(problems))


def load_state() -> dict[str, Any] | None:
    """The state saved by the last sync, or None before the first one that saved it."""
    from lestash.core.config import Config
    from lestash.core.database import get_checkpoints, get_connection

    with get_connection(Config.load()) as conn:
        return get_checkpoints(conn, "gmail").get(STATE_KEY)


def current_windows(
    queries: list[str],
) -> tuple[dict[str, Any] | None, dict[str, datetime | None]]:
    """The saved sync state, and where each search's window starts now."""
    state = load_state()
    fallback = last_successful_sync() if state is None else None
    return state, search_windows(queries, state, fallback)


def search_windows(
    queries: list[str], state: Mapping[str, Any] | None, fallback: datetime | None
) -> dict[str, datetime | None]:
    """Where each search's window starts: its last successful read (None: read everything).

    Before any state was saved, every search starts from ``fallback``, the last completed
    sync in the sync log. After that, a search not in the state is new and reads everything.
    """
    if state is None:
        return dict.fromkeys(queries, fallback)
    windows = state.get("windows", {})
    return {q: datetime.fromisoformat(windows[q]) if q in windows else None for q in queries}


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

    The run is logged ``completed`` if every search and thread was read, and ``failed``
    otherwise. Either way the searches that were read move their windows on and failed
    threads are kept for a retry, as in a sync. With ``--limit`` it is logged ``partial``
    and the sync state is left alone.

    Returns:
        (messages imported, error message or None).
    """
    from lestash.core.database import (
        get_checkpoints,
        resolve_parent_id,
        resolve_parent_source_ids,
        save_checkpoint,
        upsert_item,
    )

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
        state = get_checkpoints(conn, "gmail").get(STATE_KEY)
        lookup = partial(stored_threads, conn)
        for item in sync_queries(gmail, queries, limit=limit, state=state, stored=lookup):
            if isinstance(item, SyncCheckpoint):
                save_checkpoint(conn, "gmail", item.key, item.value)
                conn.commit()
                continue
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
            state, windows = current_windows(config["queries"])
            try:
                console.print(f"gws: {gws} — signed in as {gmail.profile().get('emailAddress')}")
                for query in config["queries"]:
                    count = sum(1 for _ in gmail.thread_ids(query))
                    start = windows[query] or "the start"
                    console.print(f"  {query}: {count} threads; next sync reads from {start}")
            except GwsError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1) from e
            if retry := (state or {}).get("retry"):
                console.print(f"[yellow]{len(retry)} thread(s) wait for a retry[/yellow]")
            if given_up := (state or {}).get("given_up"):
                console.print(f"[yellow]Given up on thread(s): {', '.join(given_up)}[/yellow]")
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

    def sync(self, config: dict) -> Iterator[ItemCreate | SyncCheckpoint]:
        """Threads with mail since each search's last successful read (all, the first time)."""
        settings = load_config()
        if not settings["queries"]:
            logger.warning("No Gmail queries configured; run `lestash gmail add-query`")
            return
        gws = find_gws(settings.get("gws_path"))
        if not gws:
            raise GwsError("gws not found; install it or set gws_path in gmail.json")
        queries = settings["queries"]
        state, since = current_windows(queries)
        yield from sync_queries(
            GwsGmail(gws), queries, since=since, state=state, stored=load_stored_threads
        )

    def configure(self) -> dict:
        return {}
