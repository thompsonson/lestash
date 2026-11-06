"""Tests for the Gmail source plugin."""

import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from lestash.models.sync import SyncCheckpoint
from lestash_gmail import client as gmail_client
from lestash_gmail.client import GwsError, GwsGmail, find_gws, load_config, save_config
from lestash_gmail.source import (
    GmailSource,
    clean_subject,
    message_text,
    search_query,
    sync_queries,
    thread_to_items,
)

QUERY = "list:uai-list.googlegroups.com"


def _items(results) -> list:
    """The items a sync yields, without its state checkpoint."""
    return [i for i in results if not isinstance(i, SyncCheckpoint)]


def test_thread_becomes_parent_with_reply_children(fake_gws) -> None:
    items = _items(sync_queries(GwsGmail("gws", runner=fake_gws), [QUERY]))
    by_id = {i.source_id: i for i in items}

    parent = by_id["gmail:thread:t1"]
    assert parent.title == "CFP: UAI 2027"
    assert parent.content == "Deadline 1 March."  # footer removed
    assert parent.author == "Alice Smith"
    assert parent.url == "https://groups.google.com/d/msgid/uai-list/abc%40googlegroups.com."
    assert parent.metadata["from_email"] == "alice@example.org"
    assert parent.metadata["list_id"] == "<uai-list.googlegroups.com>"
    assert parent.metadata["queries"] == [QUERY]
    assert parent.created_at is not None and parent.created_at.year == 2023
    assert "_parent_source_id" not in parent.metadata

    reply = by_id["gmail:message:m2"]
    assert reply.title == "Re: CFP: UAI 2027"
    assert reply.content == "Is there a workshop track?"
    assert reply.metadata["_parent_source_id"] == "gmail:thread:t1"

    # Parent is yielded before its reply
    ids = [i.source_id for i in items]
    assert ids.index("gmail:thread:t1") < ids.index("gmail:message:m2")
    assert "gmail:thread:t2" in by_id


def test_pages_through_results_and_dedupes_threads(fake_gws) -> None:
    gmail = GwsGmail("gws", runner=fake_gws)
    assert list(gmail.thread_ids(QUERY)) == ["t1", "t2"]
    lists = [c for c in fake_gws.calls if c[3:5] == ["messages", "list"]]
    assert len(lists) == 2  # followed nextPageToken


def test_sync_reads_since_last_sync_minus_margin(fake_gws) -> None:
    since = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    list(sync_queries(GwsGmail("gws", runner=fake_gws), [QUERY], since={QUERY: since}))
    first = fake_gws.calls[0]
    expected = int((since - timedelta(days=2)).timestamp())
    assert f'"q": "({QUERY}) after:{expected}"' in first[first.index("--params") + 1]


def test_first_sync_reads_everything(fake_gws) -> None:
    list(sync_queries(GwsGmail("gws", runner=fake_gws), [QUERY], since=None))
    first = fake_gws.calls[0]
    assert f'"q": "{QUERY}"' in first[first.index("--params") + 1]


def test_naive_sync_log_time_is_local() -> None:
    naive = datetime(2026, 11, 1, 12, 0)
    expected = int((naive.astimezone() - timedelta(days=2)).timestamp())
    assert search_query(QUERY, naive) == f"({QUERY}) after:{expected}"


def test_html_only_message_is_stripped() -> None:
    from lestash_gmail.source import message_text

    from tests.conftest import b64

    msg = {"payload": {"mimeType": "text/html", "body": {"data": b64("<p>Hi &amp; bye</p>")}}}
    assert message_text(msg) == "Hi & bye"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("[UAI] CFP: X", "CFP: X"),
        ("Re: [UAI] CFP: X", "CFP: X"),
        ("Re: [UAI] Re: [CFP] HSCC 2027", "[CFP] HSCC 2027"),
        ("RE: Fwd: [UAI] Jobs", "Jobs"),
        ("[EXTERNAL] Re: [UAI] y", "y"),
        ("Re: [UAI] Fwd: Re: [UAI] Talk", "Talk"),
        ("RE: AW: [UAI] x", "x"),
        ("Re:[UAI]  ", "(no subject)"),
        ("Plain", "Plain"),
        ("", "(no subject)"),
    ],
)
def test_clean_subject(raw, clean) -> None:
    assert clean_subject(raw) == clean


def test_gws_error_is_raised() -> None:
    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="error[auth]: not signed in")

    with pytest.raises(GwsError, match="not signed in"):
        GwsGmail("gws", runner=failing).profile()


def test_find_gws_prefers_configured(tmp_path, monkeypatch) -> None:
    exe = tmp_path / "gws"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(gmail_client.shutil, "which", lambda name: None)
    monkeypatch.setattr(gmail_client, "GWS_FALLBACKS", [])
    assert find_gws(str(exe)) == str(exe)
    assert find_gws(None) is None


def test_config_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gmail_client, "get_config_path", lambda: tmp_path / "gmail.json")
    assert load_config() == {"queries": []}
    save_config({"queries": [QUERY]})
    assert load_config()["queries"] == [QUERY]


def test_sync_without_queries_yields_nothing(monkeypatch) -> None:
    monkeypatch.setattr("lestash_gmail.source.load_config", lambda: {"queries": []})
    assert list(GmailSource().sync({})) == []


def test_quota_errors_are_retried_then_succeed() -> None:
    answers = [
        '{"error": {"code": 429, "message": "Quota exceeded for quota metric Units per minute",'
        ' "reason": "rateLimitExceeded"}}',
        '{"emailAddress": "me@example.org"}',
    ]
    slept: list[int] = []

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout=answers.pop(0), stderr="")

    gmail = GwsGmail("gws", runner=runner, sleep=slept.append)
    assert gmail.profile()["emailAddress"] == "me@example.org"
    assert slept == [15]


def test_other_errors_are_not_retried() -> None:
    slept: list[int] = []

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout='{"error": {"code": 404, "message": "Not found"}}'
        )

    with pytest.raises(GwsError, match="Not found"):
        GwsGmail("gws", runner=runner, sleep=slept.append).profile()
    assert slept == []


def test_footer_without_separator_is_removed() -> None:
    from lestash_gmail.source import message_text

    from tests.conftest import b64

    body = "Hello.\nYou received this message because you are subscribed to the Google Groups x."
    msg = {"payload": {"mimeType": "text/plain", "body": {"data": b64(body)}}}
    assert message_text(msg) == "Hello."


def _text(body: str) -> str:
    from tests.conftest import b64

    return message_text({"payload": {"mimeType": "text/plain", "body": {"data": b64(body)}}})


GROUPS_FOOTER = (
    "-- \nYou received this message because you are subscribed to the Google Groups "
    '"UAI" group.\nTo unsubscribe from this group, send email to '
    "uai+unsubscribe@googlegroups.com."
)


def test_bottom_posted_reply_keeps_its_text() -> None:
    quoted = "> " + GROUPS_FOOTER.replace("\n", "\n> ")
    body = (
        "On Mon, Alice wrote:\n> Is the deadline Friday?\n> \n"
        + quoted
        + "\n\nYes, Friday 23:59 AoE.\n\n"
        + GROUPS_FOOTER
    )
    text = _text(body)
    assert text.endswith("Yes, Friday 23:59 AoE.")
    assert "> Is the deadline Friday?" in text
    assert "You received this message" not in text  # quoted and trailing footers both gone


def test_top_posted_reply_drops_trailing_footer_only() -> None:
    body = "Sounds good.\n\nOn Mon, Alice wrote:\n> Is it Friday?\n\n" + GROUPS_FOOTER
    assert _text(body) == "Sounds good.\n\nOn Mon, Alice wrote:\n> Is it Friday?"


def test_empty_quote_lines_at_the_end_are_trimmed() -> None:
    assert _text("Yes.\n> earlier\n>\n> \n") == "Yes.\n> earlier"


def test_outlook_separator_keeps_the_reply() -> None:
    body = (
        "Thanks, see the attached slides.\n\n"
        + "_" * 32
        + "\nFrom: Bob\nSent: Monday\nTo: UAI\nSubject: Slides\n\n"
        "Original text about the mailing list.\n\nMy inline answer below the separator.\n"
        "More detail.\nAnd more."
    )
    text = _text(body)
    assert "My inline answer below the separator." in text
    assert text.endswith("And more.")


def test_short_underscore_list_footer_is_removed() -> None:
    body = "Hello all.\n" + "_" * 40 + "\nUAI mailing list\nTo unsubscribe visit example.org"
    assert _text(body) == "Hello all."


def test_footer_sentence_in_the_middle_is_kept() -> None:
    body = (
        "Why does the list say 'You received this message because you are subscribed to the "
        "Google Groups'?\n> quoted\nPlease explain."
    )
    assert _text(body).endswith("Please explain.")


def _msg(mid, when, subject, *, list_id=True, to="uai-list@googlegroups.com"):
    from tests.conftest import message

    m = message(mid, "t9", when, subject, f"body {mid}", "X <x@example.org>")
    headers = [h for h in m["payload"]["headers"] if h["name"] != "List-ID" or list_id]
    headers.append({"name": "To", "value": to})
    m["payload"]["headers"] = headers
    return m


def test_off_list_replies_are_left_out() -> None:
    thread = {
        "id": "t9",
        "messages": [
            _msg("a", 1, "[UAI] Talk"),
            _msg("b", 2, "Re: [UAI] Talk", list_id=False, to="Alice <alice@example.org>"),
            # Your own post: the Sent copy has no List-Id, but the list is a recipient
            _msg("c", 3, "Re: [UAI] Talk", list_id=False),
        ],
    }
    ids = [i.source_id for i in thread_to_items(thread, QUERY)]
    assert ids == ["gmail:thread:t9", "gmail:message:c"]


def test_first_on_list_message_becomes_the_parent() -> None:
    thread = {
        "id": "t9",
        "messages": [
            _msg("a", 1, "Talk", list_id=False, to="bob@example.org"),
            _msg("b", 2, "Fwd: [UAI] Talk"),
            _msg("c", 3, "Re: [UAI] Talk"),
        ],
    }
    items = thread_to_items(thread, QUERY)
    assert [i.source_id for i in items] == ["gmail:thread:t9", "gmail:message:c"]
    assert items[0].content == "body b"
    assert items[1].metadata["_parent_source_id"] == "gmail:thread:t9"


def test_non_list_queries_keep_the_whole_thread() -> None:
    thread = {"id": "t9", "messages": [_msg("a", 1, "Hi", list_id=False, to="me@example.org")]}
    assert len(thread_to_items(thread, "from:alice@example.org")) == 1


def test_list_tag_comes_from_the_thread_root() -> None:
    thread = {
        "id": "t9",
        "messages": [
            _msg("a", 1, "[UAI] [CFP] Workshop"),
            _msg("b", 2, "Re: [UAI] [CFP] Workshop"),
        ],
    }
    items = thread_to_items(thread, QUERY)
    assert [i.title for i in items] == ["[CFP] Workshop", "Re: [CFP] Workshop"]


def test_labels_are_not_stored(fake_gws) -> None:
    items = _items(sync_queries(GwsGmail("gws", runner=fake_gws), [QUERY]))
    assert all("labels" not in i.metadata for i in items)


def test_timeout_and_missing_binary_raise_gws_error() -> None:
    def hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    with pytest.raises(GwsError, match="timed out"):
        GwsGmail("gws", runner=hangs).profile()
    with pytest.raises(GwsError, match="could not run"):
        GwsGmail("gws", runner=missing).profile()


def test_unreadable_thread_is_skipped_then_sync_fails(fake_gws) -> None:
    def flaky(cmd, **kwargs):
        if "threads" in cmd and '"id": "t1"' in cmd[-1]:
            raise subprocess.TimeoutExpired(cmd, 120)
        return fake_gws(cmd, **kwargs)

    items = []
    with pytest.raises(GwsError, match="1 thread"):
        for item in sync_queries(GwsGmail("gws", runner=flaky), [QUERY]):
            items.append(item)
    assert [i.source_id for i in _items(items)] == ["gmail:thread:t2"]


@pytest.mark.parametrize(
    ("error", "retried"),
    [
        ({"code": 429, "message": "Too many requests"}, True),
        ({"code": 403, "message": "x", "reason": "userRateLimitExceeded"}, True),
        ({"code": 403, "message": "x", "errors": [{"reason": "rateLimitExceeded"}]}, True),
        ({"code": 403, "message": "Quota exceeded for quota metric"}, True),
        ({"code": 404, "message": "Message 429 not found"}, False),
        ({"code": 400, "message": "Invalid id 4291"}, False),
    ],
)
def test_rate_limit_detection_uses_the_structured_error(error, retried) -> None:
    import json

    answers = [json.dumps({"error": error}), '{"emailAddress": "me@example.org"}']
    slept: list[int] = []

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout=answers.pop(0), stderr="")

    gmail = GwsGmail("gws", runner=runner, sleep=slept.append)
    if retried:
        assert gmail.profile()["emailAddress"] == "me@example.org"
        assert slept == [15]
    else:
        with pytest.raises(GwsError):
            gmail.profile()
        assert slept == []


def test_text_errors_mentioning_429_are_not_retried() -> None:
    slept: list[int] = []

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: thread 429abc broken")

    with pytest.raises(GwsError):
        GwsGmail("gws", runner=runner, sleep=slept.append).profile()
    assert slept == []


def test_sync_window_comes_from_the_sync_log(tmp_path, monkeypatch) -> None:
    from lestash.core.config import Config, GeneralConfig
    from lestash.core.database import get_connection, init_database
    from lestash_gmail import source as gmail_source

    config = Config(general=GeneralConfig(database_path=str(tmp_path / "t.db")))
    init_database(config)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    assert gmail_source.last_successful_sync() is None

    with get_connection(config) as conn:
        conn.executemany(
            "INSERT INTO sync_log (source_type, started_at, status) VALUES (?, ?, ?)",
            [
                ("gmail", "2026-10-01 08:00:00", "completed"),
                ("gmail", "2026-10-09 08:00:00", "failed"),
                ("other", "2026-10-20 08:00:00", "completed"),
            ],
        )
        conn.commit()
    assert gmail_source.last_successful_sync() == datetime(2026, 10, 1, 8)


def test_sync_without_gws_fails_loudly(monkeypatch) -> None:
    monkeypatch.setattr("lestash_gmail.source.load_config", lambda: {"queries": [QUERY]})
    monkeypatch.setattr("lestash_gmail.source.find_gws", lambda path: None)
    with pytest.raises(GwsError, match="gws not found"):
        list(GmailSource().sync({}))


def _sync_log_db(tmp_path):
    from lestash.core.config import Config, GeneralConfig
    from lestash.core.database import get_connection, init_database

    config = Config(general=GeneralConfig(database_path=str(tmp_path / "t.db")))
    init_database(config)
    return config, get_connection


@pytest.mark.parametrize(
    ("fail_t1", "limit", "status", "items"),
    [
        (False, None, "completed", 3),
        (True, None, "failed", 1),
        (False, 1, "partial", 2),
    ],
)
def test_backfill_records_sync_log(tmp_path, monkeypatch, fake_gws, fail_t1, limit, status, items):
    from lestash.core.config import Config
    from lestash_gmail import source as gmail_source

    config, get_connection = _sync_log_db(tmp_path)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))

    def runner(cmd, **kwargs):
        if fail_t1 and "threads" in cmd and '"id": "t1"' in cmd[-1]:
            raise subprocess.TimeoutExpired(cmd, 120)
        return fake_gws(cmd, **kwargs)

    with get_connection(config) as conn:
        count, error = gmail_source.run_backfill(
            conn, GwsGmail("gws", runner=runner), [QUERY], limit=limit
        )
        row = conn.execute(
            "SELECT source_type, status, items_added, started_at, completed_at, error_message"
            " FROM sync_log"
        ).fetchone()
        stored = conn.execute("SELECT COUNT(*) FROM items WHERE source_type = 'gmail'").fetchone()
    assert count == items == stored[0]
    assert row[0] == "gmail" and row[1] == status and row[2] == items
    assert row[3] and row[4]
    assert (error is None) == (status == "completed") == (row[5] is None)

    # The sync log's window (used until sync state is saved) moves only when complete
    since = gmail_source.last_successful_sync()
    if status == "completed":
        assert since == datetime.fromisoformat(row[3])
    else:
        assert since is None


@pytest.mark.parametrize(
    ("subject", "list_id", "title"),
    [
        # The tag names the list: it is the list's and is dropped
        ("[UAI] Talk", "<uai-list.googlegroups.com>", "Talk"),
        ("[ML News] Talk", "ML News <announce.lists.example.org>", "Talk"),
        # A list without a subject tag keeps a real one
        (
            "[UAI 2026] Deadline extended",
            "<uai-list.googlegroups.com>",
            "[UAI 2026] Deadline extended",
        ),
        ("[CFP] Workshop", "<uai-list.googlegroups.com>", "[CFP] Workshop"),
    ],
)
def test_list_tag_must_name_the_list(subject, list_id, title) -> None:
    root = _msg("a", 1, subject)
    for h in root["payload"]["headers"]:
        if h["name"] == "List-ID":
            h["value"] = list_id
    [item] = thread_to_items({"id": "t9", "messages": [root]}, "from:x@example.org")
    assert item.title == title


def test_list_tag_from_the_search_when_the_root_has_no_list_id() -> None:
    # Your own post as the root: the Sent copy has no List-Id
    thread = {"id": "t9", "messages": [_msg("a", 1, "[UAI] Talk", list_id=False)]}
    assert thread_to_items(thread, QUERY)[0].title == "Talk"


def test_failed_search_is_skipped_then_sync_fails(fake_gws) -> None:
    bad = "list:broken("

    def flaky(cmd, **kwargs):
        if "messages" in cmd and "broken" in cmd[-1]:
            import json

            error = {"error": {"code": 400, "message": "Invalid query"}}
            return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps(error), stderr="")
        return fake_gws(cmd, **kwargs)

    items = []
    with pytest.raises(GwsError, match=r"1 search\(es\) failed and keep their window: list:"):
        for item in sync_queries(GwsGmail("gws", runner=flaky), [bad, QUERY]):
            items.append(item)
    assert {i.source_id for i in _items(items)} >= {"gmail:thread:t1", "gmail:thread:t2"}


def test_thread_matched_by_two_searches_is_read_once_and_stored_stably(
    tmp_path, monkeypatch, fake_gws
) -> None:
    from lestash.core.config import Config
    from lestash_gmail import source as gmail_source

    config, get_connection = _sync_log_db(tmp_path)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    other = "from:alice@example.org"
    gmail = GwsGmail("gws", runner=fake_gws)
    history = []
    with get_connection(config) as conn:
        for _ in range(3):
            fake_gws.calls.clear()
            gmail_source.run_backfill(conn, gmail, [QUERY, other])
            history.append(conn.execute("SELECT COUNT(*) FROM item_history").fetchone()[0])
            reads = [c for c in fake_gws.calls if c[3:5] == ["threads", "get"]]
            assert len(reads) == 2  # t1 and t2, once each
        row = conn.execute(
            "SELECT metadata FROM items WHERE source_id = 'gmail:thread:t1'"
        ).fetchone()
    assert history == [0, 0, 0]  # unchanged threads leave no history
    import json

    assert json.loads(row[0])["queries"] == sorted([QUERY, other])


def test_a_non_list_search_on_the_same_thread_keeps_off_list_replies() -> None:
    thread = {
        "id": "t9",
        "messages": [
            _msg("a", 1, "[UAI] Talk"),
            _msg("b", 2, "Re: [UAI] Talk", list_id=False, to="Alice <alice@example.org>"),
        ],
    }
    assert len(thread_to_items(thread, [QUERY])) == 1
    items = thread_to_items(thread, [QUERY, "from:x@example.org"])
    assert [i.source_id for i in items] == ["gmail:thread:t9", "gmail:message:b"]


def _cli_sync(tmp_path, monkeypatch, queries, runner):
    """Run `lestash sources sync gmail` through the real CLI loop, on a throwaway DB."""
    from lestash.cli import sources as sources_cli
    from lestash.core.config import Config
    from lestash_gmail import source as gmail_source

    config, get_connection = _sync_log_db(tmp_path)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"gmail": GmailSource()})
    monkeypatch.setattr(gmail_source, "load_config", lambda: {"queries": queries})
    monkeypatch.setattr(gmail_source, "find_gws", lambda path: "gws")
    monkeypatch.setattr(gmail_source, "GwsGmail", lambda path: GwsGmail(path, runner=runner))

    def sync():
        sources_cli.sync_source(source_name="gmail", all_sources=False)

    return sync, lambda: get_connection(config)


def _searches(calls) -> list[str]:
    import json

    return [json.loads(c[-1])["q"] for c in calls if c[3:5] == ["messages", "list"]]


def test_a_failing_search_does_not_freeze_the_healthy_ones(tmp_path, monkeypatch, fake_gws) -> None:
    import json

    bad = "list:broken("
    broken_searches: list[str] = []

    def runner(cmd, **kwargs):
        if "messages" in cmd and "broken" in cmd[-1]:
            broken_searches.extend(_searches([cmd]))
            error = {"error": {"code": 400, "message": "Invalid query"}}
            return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps(error), stderr="")
        return fake_gws(cmd, **kwargs)

    sync, connect = _cli_sync(tmp_path, monkeypatch, [bad, QUERY], runner)
    sync()
    fake_gws.calls.clear()
    sync()

    # The healthy search moved on from the first sync; the broken one still reads everything
    healthy = _searches(fake_gws.calls)
    assert len(healthy) == 2 and all(q.startswith(f"({QUERY}) after:") for q in healthy)
    assert broken_searches == [bad, bad]
    with connect() as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM sync_log ORDER BY id")]
        errors = [r[0] for r in conn.execute("SELECT error_message FROM sync_log")]
    assert statuses == ["failed", "failed"]  # the failure stays visible
    assert all(bad in e for e in errors)


def test_cli_sync_commits_the_other_items_when_a_thread_is_skipped(
    tmp_path, monkeypatch, fake_gws
) -> None:
    def runner(cmd, **kwargs):
        if "threads" in cmd and '"id": "t1"' in cmd[-1]:
            raise subprocess.TimeoutExpired(cmd, 120)
        return fake_gws(cmd, **kwargs)

    sync, connect = _cli_sync(tmp_path, monkeypatch, [QUERY], runner)
    sync()
    with connect() as conn:
        stored = [r[0] for r in conn.execute("SELECT source_id FROM items")]
        status, error = conn.execute("SELECT status, error_message FROM sync_log").fetchone()
        from lestash.core.database import get_checkpoints

        state = get_checkpoints(conn, "gmail")["state"]
    assert stored == ["gmail:thread:t2"]
    assert status == "failed" and "1 thread(s) could not be read" in error
    assert state["retry"] == {"t1": {"queries": [QUERY], "attempts": 1}}
    assert QUERY in state["windows"]


def test_an_unreadable_thread_is_retried_then_given_up(fake_gws) -> None:
    from lestash_gmail.source import MAX_THREAD_ATTEMPTS, search_windows

    t1_works = False

    def runner(cmd, **kwargs):
        if "messages" in cmd and "after:" in cmd[-1]:
            # Later windows match nothing: t1 is read again only from the retry list
            return subprocess.CompletedProcess(cmd, 0, stdout='{"messages": []}', stderr="")
        if "threads" in cmd and '"id": "t1"' in cmd[-1] and not t1_works:
            raise subprocess.TimeoutExpired(cmd, 120)
        return fake_gws(cmd, **kwargs)

    gmail = GwsGmail("gws", runner=runner)

    def run_with(state):
        out, error = [], None
        since = search_windows([QUERY], state, None)
        try:
            for x in sync_queries(gmail, [QUERY], since=since, state=state):
                out.append(x)
        except GwsError as e:
            error = str(e)
        [checkpoint] = [x for x in out if isinstance(x, SyncCheckpoint)]
        return _items(out), checkpoint.value, error

    _, state, error = run_with(None)
    assert state["retry"]["t1"]["attempts"] == 1 and "retried next sync" in (error or "")

    # A retry that works stores the thread and clears it
    t1_works = True
    items, ok_state, error = run_with(state)
    assert error is None and ok_state["retry"] == {}
    assert {i.source_id for i in items} == {"gmail:thread:t1", "gmail:message:m2"}

    # A thread that never works is given up on, once, after MAX_THREAD_ATTEMPTS reads
    t1_works = False
    for attempt in range(2, MAX_THREAD_ATTEMPTS):
        _, state, error = run_with(state)
        assert state["retry"]["t1"]["attempts"] == attempt
    _, state, error = run_with(state)
    assert state["retry"] == {} and "gave up on 1 thread(s)" in (error or "")
    _, state, error = run_with(state)
    assert error is None


def test_backfill_keeps_the_state_with_limit_and_saves_it_without(
    tmp_path, monkeypatch, fake_gws
) -> None:
    from lestash.core.config import Config
    from lestash.core.database import get_checkpoints
    from lestash_gmail import source as gmail_source

    config, get_connection = _sync_log_db(tmp_path)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    gmail = GwsGmail("gws", runner=fake_gws)
    with get_connection(config) as conn:
        gmail_source.run_backfill(conn, gmail, [QUERY], limit=1)
        assert get_checkpoints(conn, "gmail") == {}
        gmail_source.run_backfill(conn, gmail, [QUERY])
        assert QUERY in get_checkpoints(conn, "gmail")["state"]["windows"]


def test_windows_fall_back_to_the_sync_log_until_state_is_saved() -> None:
    from lestash_gmail.source import search_windows

    logged = datetime(2026, 10, 1, 8)
    assert search_windows([QUERY], None, logged) == {QUERY: logged}
    state = {"windows": {QUERY: "2026-11-01T12:00:00+00:00"}}
    assert search_windows([QUERY, "new"], state, logged) == {
        QUERY: datetime(2026, 11, 1, 12, tzinfo=UTC),
        "new": None,  # a search added later reads everything
    }
