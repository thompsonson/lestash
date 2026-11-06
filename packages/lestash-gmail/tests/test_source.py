"""Tests for the Gmail source plugin."""

import subprocess
from datetime import UTC, datetime, timedelta

import pytest
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


def test_thread_becomes_parent_with_reply_children(fake_gws) -> None:
    items = list(sync_queries(GwsGmail("gws", runner=fake_gws), [QUERY]))
    by_id = {i.source_id: i for i in items}

    parent = by_id["gmail:thread:t1"]
    assert parent.title == "CFP: UAI 2027"
    assert parent.content == "Deadline 1 March."  # footer removed
    assert parent.author == "Alice Smith"
    assert parent.url == "https://groups.google.com/d/msgid/uai-list/abc%40googlegroups.com."
    assert parent.metadata["from_email"] == "alice@example.org"
    assert parent.metadata["list_id"] == "<uai-list.googlegroups.com>"
    assert parent.metadata["query"] == QUERY
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
    list(sync_queries(GwsGmail("gws", runner=fake_gws), [QUERY], since=since))
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
    items = list(sync_queries(GwsGmail("gws", runner=fake_gws), [QUERY]))
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
    assert [i.source_id for i in items] == ["gmail:thread:t2"]


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
