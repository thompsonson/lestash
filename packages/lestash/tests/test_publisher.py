"""Tests for the Publisher protocol + value objects.

Slice 1 of Wave 2a per `docs/ux-compose-and-categories-design.md` §7.2.
At this slice there is no Publisher implementation yet, so the tests
exercise:

- The three frozen dataclasses (`ComposeRequest`, `LintFinding`,
  `PublishResult`) are constructible, immutable, and hashable.
- The three exception classes carry the right payloads.
- A structurally-conformant dummy class satisfies `isinstance(x, Publisher)`
  via `@runtime_checkable`.

The behavioural tests from §7.1 (lint findings, publish exceptions on 4xx/5xx,
`if_not_already_published`) land in slice 2 alongside the micro.blog adapter.
"""

from __future__ import annotations

import dataclasses

import pytest
from lestash.plugins import (
    AlreadyPublished,
    ComposeRequest,
    LintFinding,
    Publisher,
    PublishFailed,
    PublishRejected,
    PublishResult,
)


class TestComposeRequest:
    def test_constructs_with_required_fields(self) -> None:
        req = ComposeRequest(
            item_id=42,
            title="Hello",
            body="world",
            image_url=None,
        )
        assert req.item_id == 42
        assert req.categories == ()
        assert req.visibility == "public"

    def test_is_frozen(self) -> None:
        req = ComposeRequest(item_id=1, title=None, body="b", image_url=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            req.body = "tampered"  # type: ignore[misc]

    def test_categories_default_is_empty_tuple_not_shared(self) -> None:
        a = ComposeRequest(item_id=1, title=None, body="x", image_url=None)
        b = ComposeRequest(item_id=2, title=None, body="y", image_url=None)
        # default_factory means each instance gets its own object — verify
        # they're equal but the type is tuple (not a list re-used across).
        assert a.categories == () == b.categories
        assert isinstance(a.categories, tuple)

    def test_categories_accepts_tuple(self) -> None:
        req = ComposeRequest(
            item_id=1,
            title=None,
            body="b",
            image_url=None,
            categories=("life", "reading"),
        )
        assert req.categories == ("life", "reading")

    def test_hashable_for_use_in_sets(self) -> None:
        req = ComposeRequest(item_id=1, title=None, body="b", image_url=None)
        # frozen dataclasses are hashable by default — needed for memoisation
        # and dedup in the route.
        assert {req, req} == {req}


class TestLintFinding:
    def test_constructs_minimal(self) -> None:
        f = LintFinding(
            line=1,
            col=0,
            code="YT_RAW_URL",
            severity="warn",
            message="raw URL",
        )
        assert f.fix_hint is None

    def test_carries_fix_hint(self) -> None:
        f = LintFinding(
            line=4,
            col=0,
            code="YT_RAW_URL",
            severity="warn",
            message="raw URL",
            fix_hint='{{< yt "X" >}}',
        )
        assert f.fix_hint == '{{< yt "X" >}}'

    def test_is_frozen(self) -> None:
        f = LintFinding(line=1, col=0, code="YT_RAW_URL", severity="warn", message="m")
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.line = 99  # type: ignore[misc]


class TestPublishResult:
    def test_constructs(self) -> None:
        r = PublishResult(
            url="https://matt.thompson.gr/2026/06/05/x.html",
            target="microblog",
            raw_response={"url": "https://matt.thompson.gr/2026/06/05/x.html"},
        )
        assert r.target == "microblog"
        assert r.raw_response["url"].endswith("x.html")

    def test_is_frozen(self) -> None:
        r = PublishResult(url="u", target="microblog", raw_response={})
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.url = "other"  # type: ignore[misc]


class TestPublishExceptions:
    def test_already_published_is_distinct_exception(self) -> None:
        with pytest.raises(AlreadyPublished):
            raise AlreadyPublished("already there")

    def test_already_published_is_not_publish_rejected(self) -> None:
        # The §7.1 test list requires three distinct exception types so the
        # calling code can branch on them. AlreadyPublished must not be a
        # subclass of either of the others.
        assert not issubclass(AlreadyPublished, PublishRejected)
        assert not issubclass(AlreadyPublished, PublishFailed)
        assert not issubclass(PublishRejected, PublishFailed)
        assert not issubclass(PublishFailed, PublishRejected)

    def test_publish_rejected_carries_message_and_raw(self) -> None:
        raw = {"error": "missing scope"}
        try:
            raise PublishRejected("missing scope", raw)
        except PublishRejected as e:
            assert e.message == "missing scope"
            assert e.raw is raw
            assert str(e) == "missing scope"

    def test_publish_failed_carries_message_and_optional_raw(self) -> None:
        # raw=None for pre-response failures (DNS, timeout, ...)
        try:
            raise PublishFailed("timeout")
        except PublishFailed as e:
            assert e.message == "timeout"
            assert e.raw is None

        raw = {"server": "down"}
        try:
            raise PublishFailed("server error", raw)
        except PublishFailed as e:
            assert e.raw is raw


class TestPublisherProtocol:
    def test_structurally_conformant_class_satisfies_protocol(self) -> None:
        class _DummyPublisher:
            target: str = "dummy"

            def lint(self, compose: ComposeRequest) -> list[LintFinding]:
                return []

            async def publish(
                self,
                compose: ComposeRequest,
                *,
                if_not_already_published: bool = False,
            ) -> PublishResult:
                return PublishResult(url="u", target=self.target, raw_response={})

        assert isinstance(_DummyPublisher(), Publisher)

    def test_class_missing_publish_does_not_satisfy_protocol(self) -> None:
        class _IncompletePublisher:
            target: str = "broken"

            def lint(self, compose: ComposeRequest) -> list[LintFinding]:
                return []

            # NOTE: no publish method

        # runtime_checkable Protocol checks attribute presence, not signatures.
        # Missing the publish attribute entirely is what we can detect here.
        assert not isinstance(_IncompletePublisher(), Publisher)
