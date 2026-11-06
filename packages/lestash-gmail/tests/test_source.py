"""Tests for the Gmail source plugin."""

import subprocess

import pytest
from lestash_gmail import client as gmail_client
from lestash_gmail.client import GwsError, GwsGmail, find_gws, load_config, save_config
from lestash_gmail.source import RECENT_WINDOW, GmailSource, clean_subject, sync_queries

QUERY = "list:uai-list.googlegroups.com"


def test_thread_becomes_parent_with_reply_children(fake_gws) -> None:
    items = list(sync_queries(GwsGmail("gws", runner=fake_gws), [QUERY], full=True))
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


def test_recent_sync_limits_search_window(fake_gws) -> None:
    list(sync_queries(GwsGmail("gws", runner=fake_gws), [QUERY], full=False))
    first = fake_gws.calls[0]
    assert f'"q": "({QUERY}) {RECENT_WINDOW}"' in first[first.index("--params") + 1]


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
        '{"error": {"message": "Quota exceeded for quota metric Units per minute per user"}}',
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
        return subprocess.CompletedProcess(cmd, 1, stdout='{"error": {"message": "Not found"}}')

    with pytest.raises(GwsError, match="Not found"):
        GwsGmail("gws", runner=runner, sleep=slept.append).profile()
    assert slept == []


def test_footer_without_separator_is_removed() -> None:
    from lestash_gmail.source import message_text

    from tests.conftest import b64

    body = "Hello.\nYou received this message because you are subscribed to the Google Groups x."
    msg = {"payload": {"mimeType": "text/plain", "body": {"data": b64(body)}}}
    assert message_text(msg) == "Hello."
