"""Publisher protocol — the contract for posting an item from LeStash to an
external destination (micro.blog, LinkedIn, ...).

Slice 1 of Wave 2a per `docs/ux-compose-and-categories-design.md` §7.2.
Contains only the contract and its value objects. The micro.blog adapter
(`MicropubClient.create_entry`) lands in slice 2; the HTTP route in slice 3.

Type-precision conventions per the `feedback_type_precision` memory:

- `Publisher.target` is `ClassVar[str]` — a per-implementation constant, not
  a per-instance field. Allows a registry to look up the adapter by name.
- `PublishResult.raw_response` is `Mapping[str, Any]` — a read-only view of
  the provider's response body. The route serialises it into the
  `syndications` audit row; nobody should mutate the audit blob.
- All value objects are `@dataclass(frozen=True)` so they're hashable and
  safe to pass across boundaries.

The three publish exceptions exist because the §7.1 test list requires the
calling code to take three distinct branches (re-publish flag, show-to-user,
retry). Collapsing them to one base class would lose those distinctions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

LintCode = Literal[
    "YT_RAW_URL",  # raw YouTube URL where {{< yt "ID" >}} shortcode is expected
    "IMG_NOT_MARKDOWN",  # <img src="..."> instead of ![](...)
    "SOFT_CHAR_LIMIT",  # target-specific length advisory
]

Severity = Literal["info", "warn", "error"]

Visibility = Literal["public", "draft"]


@dataclass(frozen=True)
class ComposeRequest:
    """The shape the composer hands to a Publisher.

    `categories` is `tuple[str, ...]` because the dataclass is frozen and a
    mutable list would defeat that. Construct with `tuple(["a", "b"])` if
    you have a list to hand.
    """

    item_id: int
    title: str | None
    body: str
    image_url: str | None
    categories: tuple[str, ...] = field(default_factory=tuple)
    visibility: Visibility = "public"


@dataclass(frozen=True)
class LintFinding:
    """One finding from `Publisher.lint`. `fix_hint` is the concrete
    replacement string, not advice — the UI's one-click fix is a string
    replace, no extra logic.
    """

    line: int  # 1-indexed
    col: int  # 1-indexed; 0 = whole line
    code: LintCode
    severity: Severity
    message: str
    fix_hint: str | None = None


@dataclass(frozen=True)
class PublishResult:
    """Returned from `Publisher.publish` on success.

    `raw_response` is `Mapping[str, Any]` so callers know not to mutate the
    blob. The HTTP route persists it verbatim into `syndications.response_body`
    as the audit trail.
    """

    url: str
    target: str
    raw_response: Mapping[str, Any]


class AlreadyPublished(Exception):
    """Raised when this item has already been published to this target and
    the caller did not opt in to re-publish via `if_not_already_published`."""


class PublishRejected(Exception):
    """Raised on a 4xx response from the provider. `message` is the
    provider-supplied human-readable string; callers typically show it to
    the user verbatim. `raw` is the parsed response body for the audit."""

    def __init__(self, message: str, raw: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw


class PublishFailed(Exception):
    """Raised on 5xx, network failure, or other non-deterministic failure
    the caller may retry. `raw` is None when the failure was pre-response."""

    def __init__(self, message: str, raw: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw


@runtime_checkable
class Publisher(Protocol):
    """Contract for any system LeStash can publish an item to.

    `target` is a class-level constant identifying the destination
    ("microblog", "linkedin", ...). The registry looks adapters up by it.

    `lint` is sync and pure — no IO, no network — so it can run on every
    keystroke. The `publish` call is async because it talks to the network.
    """

    target: ClassVar[str]

    def lint(self, compose: ComposeRequest) -> list[LintFinding]:
        """Return per-line findings for the composed body. Pure function."""
        ...

    async def publish(
        self,
        compose: ComposeRequest,
        *,
        if_not_already_published: bool = False,
    ) -> PublishResult:
        """Publish the composed item to this target.

        Raises:
            AlreadyPublished: prior publish exists and the flag is False.
            PublishRejected:  provider returned 4xx — show `message` to user.
            PublishFailed:    network / 5xx — may be retried.
        """
        ...
