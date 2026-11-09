"""Tests for YouTube Takeout watch/search history parsing."""

from lestash.importers.youtube_history import parse_search_history, parse_watch_history


def watch(video_id: str, when: str, title: str = "A talk", channel: str | None = "Chan", **extra):
    entry = {
        "header": "YouTube",
        "title": f"Watched {title}",
        "titleUrl": f"https://www.youtube.com/watch?v={video_id}",
        "time": when,
        "products": ["YouTube"],
    }
    if channel:
        entry["subtitles"] = [{"name": channel, "url": "https://www.youtube.com/channel/UC1"}]
    entry.update(extra)
    return entry


def test_rewatches_become_one_item_with_all_times() -> None:
    items = parse_watch_history(
        [
            watch("AAAAAAAAAAA", "2026-01-22T14:36:35.783Z", title="New title"),
            watch("AAAAAAAAAAA", "2025-06-01T10:00:00Z", title="Old title"),
            watch("BBBBBBBBBBB", "2025-07-01T10:00:00Z"),
        ]
    )
    by_id = {i.source_id: i for i in items}
    assert set(by_id) == {"history:AAAAAAAAAAA", "history:BBBBBBBBBBB"}
    a = by_id["history:AAAAAAAAAAA"]
    assert a.title == "New title"  # latest title wins
    assert a.author == "Chan"
    assert a.url == "https://www.youtube.com/watch?v=AAAAAAAAAAA"
    assert a.created_at.isoformat().startswith("2025-06-01")  # first watch
    assert a.metadata["watched_count"] == 2
    assert "watched_times" not in a.metadata  # the times are activities
    assert [(x.verb, x.occurred_at.isoformat(), x.source) for x in a.activities] == [
        ("watched", "2025-06-01T10:00:00+00:00", "takeout:youtube"),
        ("watched", "2026-01-22T14:36:35.783000+00:00", "takeout:youtube"),
    ]
    assert a.metadata["last_at"].startswith("2026-01-22")
    assert a.metadata["source_subtype"] == "history"
    # Nest under the liked video when there is one
    assert a.metadata["_parent_source_id"] == "liked:AAAAAAAAAAA"
    assert "Watched 2 times: 2026-01-22, 2025-06-01" in a.content
    assert "Channel: Chan" in a.content


def test_ads_removed_videos_and_posts() -> None:
    items = parse_watch_history(
        [
            watch("CCCCCCCCCCC", "2026-01-01T00:00:00Z", details=[{"name": "From Google Ads"}]),
            watch(
                "DDDDDDDDDDD",
                "2026-01-02T00:00:00Z",
                title="https://www.youtube.com/watch?v=DDDDDDDDDDD",
                channel=None,
            ),
            {
                "header": "YouTube",
                "title": "Viewed Hi community,\nA new survey on agentic reasoning.",
                "titleUrl": "https://www.youtube.com/post/UgkxABC",
                "subtitles": [{"name": "AI Channel", "url": "https://www.youtube.com/channel/UC2"}],
                "time": "2026-01-03T00:00:00Z",
            },
        ]
    )
    by_id = {i.source_id: i for i in items}
    assert "history:CCCCCCCCCCC" not in by_id  # ad
    gone = by_id["history:DDDDDDDDDDD"]
    assert gone.title == "(video no longer available)"
    assert gone.metadata["unavailable"] is True
    post = by_id["post:UgkxABC"]
    assert post.title == "Hi community,"
    assert "agentic reasoning" in post.content
    assert post.author == "AI Channel"
    assert post.metadata["viewed_count"] == 1


def test_searches_grouped_case_insensitively() -> None:
    items = parse_search_history(
        [
            {
                "title": "Searched for markov blanket",
                "titleUrl": "https://www.youtube.com/results?search_query=markov+blanket",
                "time": "2026-01-17T23:20:58Z",
            },
            {
                "title": "Searched for Markov  Blanket",
                "titleUrl": "https://www.youtube.com/results?search_query=Markov++Blanket",
                "time": "2025-12-01T10:00:00Z",
            },
            {
                "title": "Watched an ad",
                "titleUrl": "https://www.youtube.com/watch?v=EEEEEEEEEEE",
                "time": "2025-12-01T10:00:00Z",
                "details": [{"name": "From Google Ads"}],
            },
        ]
    )
    assert len(items) == 1
    search = items[0]
    assert search.source_id == "search:markov blanket"
    assert search.title == "Searched YouTube: markov blanket"
    assert search.metadata["searched_count"] == 2
    assert search.is_own_content is True
    assert "2 times" in search.content


def test_youtube_music_is_left_for_later() -> None:
    music = watch("MMMMMMMMMMM", "2026-01-01T00:00:00Z", title="A podcast episode")
    music["header"] = "YouTube Music"
    assert parse_watch_history([music]) == []
    search = {
        "header": "YouTube Music",
        "title": "Searched for lex fridman",
        "titleUrl": "https://music.youtube.com/search?q=lex+fridman",
        "time": "2026-01-01T00:00:00Z",
    }
    assert parse_search_history([search]) == []


# --- My Activity: likes, dislikes and shares ------------------------------------------------


def _entry(title: str, url: str, when: str, subtitles: list | None = None) -> dict:
    entry = {"header": "YouTube", "title": title, "titleUrl": url, "time": when}
    if subtitles is not None:
        entry["subtitles"] = subtitles
    return entry


def test_likes_dislikes_and_shares_are_activities_of_the_video_item() -> None:
    url = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
    items = parse_watch_history(
        [
            watch("AAAAAAAAAAA", "2026-01-01T10:00:00Z"),
            _entry("Liked A talk", url, "2026-01-01T10:05:00Z", [{"name": "Chan"}]),
            _entry(
                "Shared video",
                "https://youtube.com/watch?v=AAAAAAAAAAA&is=xyz",
                "2026-01-02T10:00:00Z",
                [{"name": f"Shared URL: {url}"}, {"name": "A talk", "url": url}],
            ),
        ]
    )
    [video] = items
    assert video.title == "A talk"  # from the watch, not "Shared video"
    assert video.metadata["watched_count"] == 1
    assert [(a.verb, a.occurred_at.isoformat()) for a in video.activities] == [
        ("watched", "2026-01-01T10:00:00+00:00"),
        ("liked", "2026-01-01T10:05:00+00:00"),
        ("shared", "2026-01-02T10:00:00+00:00"),
    ]


def test_a_video_only_liked_or_shared_still_gets_its_item() -> None:
    liked = "https://www.youtube.com/watch?v=LLLLLLLLLLL"
    shared = "https://www.youtube.com/watch?v=SSSSSSSSSSS"
    items = parse_watch_history(
        [
            _entry("Liked Song of the day", liked, "2026-03-01T10:00:00Z", [{"name": "Band"}]),
            _entry("Disliked Song of the day", liked, "2026-02-01T10:00:00Z", [{"name": "Band"}]),
            _entry(
                "Shared video",
                f"{shared}&is=abc",
                "2026-03-02T10:00:00Z",
                [{"name": f"Shared URL: {shared}"}, {"name": "A shared talk", "url": shared}],
            ),
        ]
    )
    by_id = {i.source_id: i for i in items}
    song = by_id["history:LLLLLLLLLLL"]
    assert (song.title, song.author) == ("Song of the day", "Band")
    assert "watched_count" not in song.metadata
    assert song.created_at.isoformat().startswith("2026-02-01")  # the first activity
    assert song.content == "Song of the day\nChannel: Band"
    assert sorted(a.verb for a in song.activities) == ["disliked", "liked"]
    talk = by_id["history:SSSSSSSSSSS"]
    assert (talk.title, talk.author) == ("A shared talk", None)  # a share names no channel


def test_a_shared_video_takes_its_channel_from_a_like() -> None:
    url = "https://www.youtube.com/watch?v=SSSSSSSSSSS"
    [video] = parse_watch_history(
        [
            _entry("Liked A shared talk", url, "2026-03-01T10:00:00Z", [{"name": "Chan"}]),
            _entry(
                "Shared video",
                f"{url}&is=abc",
                "2026-03-02T10:00:00Z",
                [{"name": f"Shared URL: {url}"}, {"name": "A shared talk", "url": url}],
            ),
        ]
    )
    assert (video.title, video.author, video.metadata["channel_title"]) == (
        "A shared talk",
        "Chan",
        "Chan",
    )


def test_other_my_activity_entries_are_skipped() -> None:
    post = "https://www.youtube.com/post/UgkxPOST"
    items = parse_watch_history(
        [
            _entry("Voted on Which is best?", post, "2026-01-01T10:00:00Z"),
            _entry(
                "Subscribed to Chan", "https://www.youtube.com/channel/UC1", "2026-01-01T10:00:00Z"
            ),
            _entry(
                "Shared playlist",
                "https://youtube.com/playlist?list=PL1",
                "2026-01-01T10:00:00Z",
                [{"name": "Shared URL: x"}, {"name": "Mix"}],
            ),
        ]
    )
    assert items == []
