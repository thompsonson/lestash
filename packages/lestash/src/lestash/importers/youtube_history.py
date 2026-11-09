"""YouTube watch and search history from a Google Takeout export.

Takeout's `YouTube and YouTube Music/history/watch-history.json` and `search-history.json`
(choose JSON for history when creating the export), or `My Activity/YouTube/My Activity.json`,
which holds both and keeps them longer. One item per video, search query or community post,
with every time it happened kept as an activity, so "what was I watching in early March"
works. Videos become the canonical `history:{video_id}` item; a video you also liked is
nested under its `liked:{video_id}` item instead of appearing twice. Ads and supervised
(children's) accounts are skipped.

My Activity also records likes, dislikes and shares. Those of a video are activities of its
`history:` item, which is made for a video you never watched. Its other entries
(subscriptions, saved playlists, poll votes, dismissed suggestions) are skipped.

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
ACTION_PREFIX = re.compile(r"^(Watched|Viewed|Searched for|Liked|Disliked)\s+", re.S)
# Title word -> verb, for the entries about a video
VIDEO_VERBS = {"Watched": "watched", "Liked": "liked", "Disliked": "disliked", "Shared": "shared"}


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
        ordered = sorted(set(self.times))  # an entry listed twice is one time
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


def _video_title(entry: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """A video entry's title, channel and channel URL.

    A share's title is "Shared video": its subtitles hold the shared URL, then the video's
    title, and no channel.
    """
    if entry.get("title", "").startswith("Shared"):
        subs = entry.get("subtitles") or []
        return (subs[-1].get("name") or "" if len(subs) > 1 else ""), None, None
    channel, channel_url = _channel(entry)
    return ACTION_PREFIX.sub("", entry.get("title", "")).strip(), channel, channel_url


def parse_watch_history(entries: list[dict[str, Any]]) -> list[ItemCreate]:
    """Watched videos and viewed community posts, one item each.

    A video's likes, dislikes and shares are activities of its item too.
    """
    videos: dict[str, dict[str, _Group]] = defaultdict(lambda: defaultdict(_Group))
    posts: dict[str, _Group] = defaultdict(_Group)
    for entry in entries:
        if _is_ad(entry) or _is_music(entry):
            continue
        url = entry.get("titleUrl") or ""
        word = entry.get("title", "").split(" ", 1)[0]
        if (match := VIDEO_ID.search(url)) and word in VIDEO_VERBS:
            videos[match.group(1)][VIDEO_VERBS[word]].add(entry)
        elif (match := POST_ID.search(url)) and word == "Viewed":
            posts[match.group(1)].add(entry)

    items = []
    for video_id, groups in videos.items():
        watches = groups.get("watched") or _Group()
        # The title of the latest watch; else of the latest like, dislike or share
        named = watches if watches.entries else _latest_group(groups)
        title, channel, channel_url = _video_title(named.latest)
        unavailable = title.startswith("https://") or not title
        if unavailable:
            title = "(video no longer available)"
        product = named.latest.get("header") or "YouTube"
        every_time = [t for group in groups.values() for t in group.times]
        metadata = {
            "video_id": video_id,
            "channel_title": channel,
            "channel_url": channel_url,
            "source_subtype": "history",
            "product": product,
            "source": "takeout",
            **(watches.time_metadata("watched") if watches.entries else {}),
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
                content=video_content(title, channel, watches.times),
                author=channel,
                created_at=min(watches.times or every_time) if every_time else None,
                is_own_content=False,
                metadata={k: v for k, v in metadata.items() if v is not None},
                activities=[a for verb, group in groups.items() for a in group.activities(verb)],
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


def _latest_group(groups: dict[str, _Group]) -> _Group:
    """The group with the most recent entry."""
    return max(groups.values(), key=lambda g: g.latest.get("time", ""))


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
