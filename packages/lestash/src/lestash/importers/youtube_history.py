"""YouTube watch and search history from a Google Takeout export.

Takeout's `YouTube and YouTube Music/history/watch-history.json` and `search-history.json`
(choose JSON for history when creating the export). One item per video, search query or
community post, with every time it happened kept as an activity, so "what was I watching in early
March" works. Videos become the canonical `history:{video_id}` item; a video you also liked is
nested under its `liked:{video_id}` item instead of appearing twice. Ads and supervised
(children's) accounts are skipped.

YouTube Music entries (songs, but also podcast episodes) are skipped for now: they will get
their own item type rather than being treated as videos.
"""

import re
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from lestash.models.item import ActivityCreate, ItemCreate

SOURCE = "takeout:youtube"  # activities.source of what this importer writes
VIDEO_ID = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
POST_ID = re.compile(r"/post/([A-Za-z0-9_-]+)")
ACTION_PREFIX = re.compile(r"^(Watched|Viewed|Searched for)\s+", re.S)


def _is_ad(entry: dict[str, Any]) -> bool:
    return any("Google Ads" in (d.get("name") or "") for d in entry.get("details") or [])


def _is_music(entry: dict[str, Any]) -> bool:
    """YouTube Music listens (songs and podcasts), handled separately later."""
    return entry.get("header") == "YouTube Music"


def _time(entry: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return None


def _channel(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    subs = entry.get("subtitles") or []
    if not subs:
        return None, None
    return subs[0].get("name"), subs[0].get("url")


def video_content(
    title: str, channel: str | None, times: list[datetime], description: str | None = None
) -> str:
    """Content of a watched-video item; also rebuilt when a re-import merges watch times."""
    lines = [title]
    if channel:
        lines.append(f"Channel: {channel}")
    if times:
        lines.append(f"Watched {_times_summary(times)}")
    if description:
        lines += ["", description]
    return "\n".join(lines)


def search_content(query: str, times: list[datetime]) -> str:
    """Content of a search item; also rebuilt when a re-import merges search times."""
    if not times:
        return f"Searched YouTube for “{query}”"
    return f"Searched YouTube for “{query}” {_times_summary(times)}"


def _times_summary(times: list[datetime]) -> str:
    days = sorted({t.date().isoformat() for t in times}, reverse=True)
    shown = ", ".join(days[:10]) + (f" and {len(days) - 10} more days" if len(days) > 10 else "")
    count = f"{len(times)} times" if len(times) > 1 else "once"
    return f"{count}: {shown}"


class _Group:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.times: list[datetime] = []

    def add(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        when = _time(entry)
        if when:
            self.times.append(when)

    @property
    def latest(self) -> dict[str, Any]:
        """The most recent entry (has the current title for renamed videos)."""
        return max(self.entries, key=lambda e: e.get("time", ""))

    def time_metadata(self, verb: str) -> dict[str, Any]:
        """The metadata summing up the times: their count, the first and the last."""
        ordered = sorted(self.times)
        return {
            f"{verb}_count": len(ordered),
            "first_at": ordered[0].isoformat() if ordered else None,
            "last_at": ordered[-1].isoformat() if ordered else None,
        }

    def activities(self, verb: str, details: dict[str, Any] | None = None) -> list[ActivityCreate]:
        """One activity per time, kept in the activities table, not in metadata."""
        return [
            ActivityCreate(verb=verb, occurred_at=t, source=SOURCE, details=details)
            for t in sorted(set(self.times))
        ]


def parse_watch_history(entries: list[dict[str, Any]]) -> list[ItemCreate]:
    """Watched videos and viewed community posts, one item each."""
    videos: dict[str, _Group] = defaultdict(_Group)
    posts: dict[str, _Group] = defaultdict(_Group)
    for entry in entries:
        if _is_ad(entry) or _is_music(entry):
            continue
        url = entry.get("titleUrl") or ""
        if (match := VIDEO_ID.search(url)) and entry.get("title", "").startswith("Watched"):
            videos[match.group(1)].add(entry)
        elif match := POST_ID.search(url):
            posts[match.group(1)].add(entry)

    items = []
    for video_id, group in videos.items():
        latest = group.latest
        title = ACTION_PREFIX.sub("", latest.get("title", "")).strip()
        unavailable = title.startswith("https://")
        if unavailable:
            title = "(video no longer available)"
        channel, channel_url = _channel(latest)
        product = latest.get("header") or "YouTube"
        metadata = {
            "video_id": video_id,
            "channel_title": channel,
            "channel_url": channel_url,
            "source_subtype": "history",
            "product": product,
            "source": "takeout",
            **group.time_metadata("watched"),
            "_parent_source_id": f"liked:{video_id}",
        }
        if unavailable:
            metadata["unavailable"] = True
        items.append(
            ItemCreate(
                source_type="youtube",
                source_id=f"history:{video_id}",
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=title,
                content=video_content(title, channel, group.times),
                author=channel,
                created_at=min(group.times) if group.times else None,
                is_own_content=False,
                metadata={k: v for k, v in metadata.items() if v is not None},
                activities=group.activities("watched"),
            )
        )

    for post_id, group in posts.items():
        latest = group.latest
        text = ACTION_PREFIX.sub("", latest.get("title", "")).strip()
        channel, channel_url = _channel(latest)
        first_line = text.splitlines()[0] if text else "Community post"
        items.append(
            ItemCreate(
                source_type="youtube",
                source_id=f"post:{post_id}",
                url=latest.get("titleUrl"),
                title=first_line[:100] + ("…" if len(first_line) > 100 else ""),
                content=text,
                author=channel,
                created_at=min(group.times) if group.times else None,
                is_own_content=False,
                metadata={
                    k: v
                    for k, v in {
                        "post_id": post_id,
                        "channel_title": channel,
                        "channel_url": channel_url,
                        "source_subtype": "post",
                        "source": "takeout",
                        **group.time_metadata("viewed"),
                    }.items()
                    if v is not None
                },
                activities=group.activities("viewed"),
            )
        )
    return items


def _query(entry: dict[str, Any]) -> str | None:
    url = entry.get("titleUrl") or ""
    values = parse_qs(urlparse(url).query).get("search_query")
    if values:
        return values[0].strip()
    title = entry.get("title", "")
    return ACTION_PREFIX.sub("", title).strip() if title.startswith("Searched for") else None


def parse_search_history(entries: list[dict[str, Any]]) -> list[ItemCreate]:
    """YouTube searches, one item per distinct query (case-insensitive)."""
    searches: dict[str, _Group] = defaultdict(_Group)
    for entry in entries:
        if _is_ad(entry) or _is_music(entry):
            continue
        query = _query(entry)
        if query:
            searches[" ".join(query.lower().split())].add(entry)

    items = []
    for key, group in searches.items():
        query = _query(group.latest) or key
        items.append(
            ItemCreate(
                source_type="youtube",
                source_id=f"search:{key}",
                url=group.latest.get("titleUrl"),
                title=f"Searched YouTube: {query}",
                content=search_content(query, group.times),
                created_at=min(group.times) if group.times else None,
                is_own_content=True,
                metadata={
                    k: v
                    for k, v in {
                        "query": query,
                        "source_subtype": "search",
                        "source": "takeout",
                        **group.time_metadata("searched"),
                    }.items()
                    if v is not None
                },
                activities=group.activities("searched", {"query": query}),
            )
        )
    return items
