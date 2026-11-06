"""Fixtures for lestash-jelu: a fake Jelu API."""

import base64
from typing import Any

import httpx
import pytest
from lestash_jelu.client import JeluClient

BASE = "https://jelu.test"

USERBOOKS: list[dict[str, Any]] = [
    {
        "id": "ub-1",
        "creationDate": "2026-01-02T10:00:00Z",
        "modificationDate": "2026-03-01T10:00:00Z",
        "lastReadingEvent": "FINISHED",
        "lastReadingEventDate": "2026-02-20T18:00:00Z",
        "personalNotes": "Watkins and TD learning.",
        "toRead": False,
        "owned": True,
        "book": {
            "id": "b-1",
            "title": "The Alignment Problem",
            "isbn13": "9780393868333",
            "amazonId": "B085T55LGK",
            "summary": "<p>Machine learning &amp; human values.</p>",
            "image": "alignment.jpg",
            "authors": [{"name": "Brian Christian"}],
            "tags": [{"name": "ai"}, {"name": "nonfiction"}],
            "series": [],
        },
    },
    {
        "id": "ub-2",
        "creationDate": "2026-04-01T10:00:00Z",
        "lastReadingEvent": None,
        "toRead": True,
        "book": {
            "id": "b-2",
            "title": "The Coming Wave",
            "authors": [{"name": "Mustafa Suleyman"}],
        },
    },
]
REVIEWS = [
    {
        "id": "r-1",
        "book": "b-1",
        "user": "u-1",
        "text": "Great on RL.",
        "rating": 9.0,
        "visibility": "PRIVATE",
        "reviewDate": "2026-02-21T08:00:00Z",
    }
]
QUOTES = [
    {
        "id": "q-1",
        "book": "b-1",
        "user": "u-1",
        "text": "Dopamine is a prediction error.",
        "position": "ch. 4",
        "creationDate": "2026-02-10T08:00:00Z",
    }
]


def _page(items: list[Any], request: httpx.Request) -> dict[str, Any]:
    """Serve items as Spring pages using the request's page/size."""
    page = int(request.url.params.get("page", 0))
    size = int(request.url.params.get("size", 20))
    chunk = items[page * size : (page + 1) * size]
    total_pages = max(1, -(-len(items) // size))
    return {"content": chunk, "totalPages": total_pages, "last": page + 1 >= total_pages}


# Paths a scoped API token may reach (Jelu's TokenScope.kt); everything else is 403.
# /users/me and /book-quotes are missing there, which is why the plugin uses a session.
BEARER_PATHS = ("/api/v1/userbooks", "/api/v1/reviews", "/api/v1/quotes", "/api/v1/books")


class FakeJelu:
    """Jelu's auth model: Basic login on /users/me returns a session token for X-Auth-Token.

    Without a live session, /reviews answers with public reviews only (it is permitAll in
    Jelu) and every other path is 401.
    """

    def __init__(self, userbooks=USERBOOKS, reviews=REVIEWS, quotes=QUOTES) -> None:
        self.userbooks, self.reviews, self.quotes = userbooks, reviews, quotes
        self.sessions: set[str] = set()
        self.logins = 0
        self.calls: list[httpx.Request] = []

    def expire_sessions(self) -> None:
        self.sessions.clear()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            if not path.startswith(BEARER_PATHS):
                return httpx.Response(403)
            return httpx.Response(401)  # no tokens issued in this fake
        if auth.startswith("Basic "):
            if path != "/api/v1/users/me":
                return httpx.Response(400, text="the plugin should only send Basic to log in")
            if auth != "Basic " + base64.b64encode(b"mt:pw").decode():
                return httpx.Response(401)
            self.logins += 1
            token = f"sess-{self.logins}"
            self.sessions.add(token)
            return httpx.Response(200, json={"user": {"id": "u-1", "login": "mt"}, "token": token})

        session = request.headers.get("X-Auth-Token")
        logged_in = session in self.sessions
        if path == "/api/v1/reviews":
            reviews = (
                self.reviews
                if logged_in
                else [r for r in self.reviews if r.get("visibility") == "PUBLIC"]
            )
            if request.url.params.get("userId"):
                reviews = [r for r in reviews if r.get("user") == request.url.params["userId"]]
            return httpx.Response(200, json=_page(reviews, request))
        if not logged_in:
            return httpx.Response(401)
        if path == "/api/v1/users/me":
            return httpx.Response(
                200, json={"user": {"id": "u-1", "login": "mt"}, "token": session}
            )
        if path == "/api/v1/userbooks":
            return httpx.Response(200, json=_page(self.userbooks, request))
        if path == "/api/v1/book-quotes":
            quotes = self.quotes
            if request.url.params.get("userId"):
                quotes = [q for q in quotes if q.get("user") == request.url.params["userId"]]
            return httpx.Response(200, json=_page(quotes, request))
        return httpx.Response(404)


@pytest.fixture
def jelu() -> FakeJelu:
    return FakeJelu()


@pytest.fixture
def client(jelu):
    with JeluClient(BASE, "mt", "pw", transport=httpx.MockTransport(jelu)) as c:
        yield c
