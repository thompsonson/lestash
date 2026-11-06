"""A fake `gws` for tests: answers Gmail calls from canned threads."""

import base64
import json
import subprocess
from typing import Any

import pytest


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


FOOTER = (
    "\n-- \nYou received this message because you are subscribed to the Google Groups "
    '"UAI Mailing List" group.\nTo view this discussion visit '
    "https://groups.google.com/d/msgid/uai-list/abc%40googlegroups.com.\n"
)


def message(
    mid: str, tid: str, when_ms: int, subject: str, body: str, sender: str
) -> dict[str, Any]:
    return {
        "id": mid,
        "threadId": tid,
        "internalDate": str(when_ms),
        "labelIds": ["INBOX", "CATEGORY_FORUMS"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Message-Id", "value": f"<{mid}@example.org>"},
                {"name": "List-ID", "value": "<uai-list.googlegroups.com>"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64(body)}},
                {"mimeType": "text/html", "body": {"data": b64(f"<p>{body}</p>")}},
            ],
        },
    }


THREADS = {
    "t1": {
        "id": "t1",
        "messages": [
            # Out of order on purpose: the oldest must become the parent
            message(
                "m2",
                "t1",
                1_700_000_100_000,
                "Re: [UAI] CFP: UAI 2027",
                "Is there a workshop track?" + FOOTER,
                "Bob <bob@example.org>",
            ),
            message(
                "m1",
                "t1",
                1_700_000_000_000,
                "[UAI] CFP: UAI 2027",
                "Deadline 1 March." + FOOTER,
                "Alice Smith <alice@example.org>",
            ),
        ],
    },
    "t2": {
        "id": "t2",
        "messages": [
            message(
                "m3",
                "t2",
                1_700_000_200_000,
                "[UAI] PhD positions",
                "Apply now.",
                "Carol <c@example.org>",
            )
        ],
    },
}


class FakeGws:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        params = json.loads(cmd[cmd.index("--params") + 1])
        sub = cmd[3 : cmd.index("--params")]
        if sub == ["getProfile"]:
            out: Any = {"emailAddress": "me@example.org"}
        elif sub == ["messages", "list"]:
            msgs = [
                {"id": m["id"], "threadId": t} for t, th in THREADS.items() for m in th["messages"]
            ]
            if params.get("pageToken") is None:
                out = {"messages": msgs[:2], "nextPageToken": "p2"}
            else:
                out = {"messages": msgs[2:]}
        elif sub == ["threads", "get"]:
            out = THREADS.get(params["id"]) or {"error": {"message": "Not found"}}
        else:
            out = {"error": {"message": f"unexpected {sub}"}}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")


@pytest.fixture
def fake_gws():
    return FakeGws()
