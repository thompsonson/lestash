"""Fixtures for lestash-jelu: a fake Jelu API."""

import json
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
            "summary": "<p>Machine learning and human values.</p>",
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


def make_handler(userbooks=USERBOOKS, reviews=REVIEWS, quotes=QUOTES, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if request.headers.get("Authorization") != "Bearer secret":
            return httpx.Response(401)
        path = request.url.path
        if path == "/api/v1/users/me":
            body: Any = {"id": "u-1", "login": "mt"}
        elif path == "/api/v1/userbooks":
            body = _page(userbooks, request)
        elif path == "/api/v1/reviews":
            assert request.url.params.get("userId") == "u-1"
            body = _page(reviews, request)
        elif path == "/api/v1/book-quotes":
            assert request.url.params.get("userId") == "u-1"
            body = _page(quotes, request)
        else:
            return httpx.Response(404)
        return httpx.Response(200, content=json.dumps(body))

    return handler


@pytest.fixture
def client():
    with JeluClient(BASE, "secret", transport=httpx.MockTransport(make_handler())) as c:
        yield c
