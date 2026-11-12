"""The LinkedIn export import: posts, comments and articles, without doubling what the
sync holds."""

import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database, upsert_item
from lestash.models.item import ItemCreate
from lestash_linkedin.importer import import_export, parse_article, post_urn_of, share_text
from lestash_linkedin.stubs import snowflake_ms

ME = "urn:li:person:me"
# Made at 2026-01-19T07:09:09.522Z
HELD_SHARE = "urn:li:share:7418912385886941184"
NEW_SHARE = "urn:li:share:7000000000000000000"
OTHER_POST = "urn:li:activity:7100000000000000000"
FEED = "https://www.linkedin.com/feed/update/"

SHARES = f'''Date,ShareLink,ShareCommentary,SharedUrl,MediaUrl,Visibility
2026-01-19 07:09:09,"{FEED}urn%3Ali%3Ashare%3A7418912385886941184","Held already",,,MEMBER_NETWORK
2024-03-30 17:02:00,"{FEED}urn%3Ali%3Ashare%3A7000000000000000000","An old post
on two lines",https://example.com,,MEMBER_NETWORK
2024-03-31 09:00:00,"{FEED}urn%3Ali%3Ashare%3A7000000000000000001",,,,MEMBER_NETWORK
'''

COMMENTS = f"""Date,Link,Message
2024-03-30 20:37:00,{FEED}urn:li:activity:7100000000000000000,"Says \\"hello\\", twice"
2024-03-30 20:37:00,{FEED}urn:li:activity:7100000000000000000,"Another in the same second"
2026-01-19 10:45:54,{FEED}urn:li:activity:7418912385886941184,"Held, with other spacing"
2026-01-19 11:00:00,{FEED}urn:li:activity:7418912385886941184,"Text the sync never gave"
"""

ARTICLE = """<html><head><title>T</title><style>p {}</style></head><body>
<h1><a href="https://www.linkedin.com/pulse/an-article-me-abc">An article</a></h1>
<p class="created">Created on 2024-02-07 20:53</p>
<p class="published">Published on 2024-02-07 21:03</p>
<div><p>First paragraph.</p><h2>Part</h2><p>Second, with a <a href="https://x.org">link</a>.</p></div>
</body></html>"""

DRAFT = """<html><body><h1>A draft</h1>
<p class="created">Created on 2021-10-21 03:23</p><p class="published">Published on ---</p>
<div><p>An outline</p></div></body></html>"""


@pytest.fixture
def config() -> Iterator[Config]:
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(general=GeneralConfig(database_path=str(Path(tmpdir) / "t.db")))
        init_database(config)
        yield config


@pytest.fixture
def export(tmp_path: Path) -> Path:
    path = tmp_path / "Basic_LinkedInDataExport.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Shares.csv", SHARES)
        zf.writestr("Comments.csv", COMMENTS)
        zf.writestr("Reactions.csv", "Date,Type,Link\n")
        zf.writestr("Articles/Articles/an-article-me-abc.html", ARTICLE)
        zf.writestr("Articles/Articles/2021-10-21 03:23:26.0-A draft.html", DRAFT)
    return path


def _held(conn) -> None:
    """What the sync holds: one post, one comment on it, one comment with no text."""
    upsert_item(
        conn,
        ItemCreate(
            source_type="linkedin",
            source_id=f"changelog-ugcPosts-{HELD_SHARE}",
            content="Held already",
            author=ME,
            is_own_content=True,
            metadata={"source": "changelog", "resource_name": "ugcPosts", "post_id": HELD_SHARE},
        ),
    )
    for n, (text, method, captured) in enumerate(
        [
            ("Held,  with other spacing!", "CREATE", 1768819554400),
            ("CREATE socialActions/comments", "CREATE", 1768820400700),
        ]
    ):
        upsert_item(
            conn,
            ItemCreate(
                source_type="linkedin",
                source_id=f"changelog-socialActions/comments-{n}",
                content=text,
                author=ME,
                is_own_content=True,
                metadata={
                    "source": "changelog",
                    "resource_name": "socialActions/comments",
                    "method": method,
                    "commented_on": "urn:li:activity:7418912385886941184",
                    # processedAt is often 30 s late; capturedAt is close
                    "raw": {"capturedAt": captured, "processedAt": captured + 30000},
                },
            ),
        )


def _items(conn, where: str) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM items WHERE {where} ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def test_post_urn_of_reads_encoded_links() -> None:
    assert post_urn_of(f"{FEED}urn%3Ali%3Ashare%3A12/") == "urn:li:share:12"
    assert post_urn_of(f"{FEED}urn:li:groupPost:123-456") == "urn:li:groupPost:123-456"
    assert post_urn_of("https://example.com") is None


def test_import_adds_what_the_sync_lacks(config: Config, export: Path) -> None:
    with get_connection(config) as conn:
        _held(conn)
        report = import_export(conn, export)
        assert (report.posts_added, report.posts_held) == (1, 1)
        assert (report.comments_added, report.comments_held) == (2, 2)
        assert (report.articles_added, report.filled, report.skipped) == (2, 1, 1)

        (post,) = _items(
            conn,
            "json_extract(metadata, '$.source') = 'export' "
            "AND json_extract(metadata, '$.resource_name') = 'ugcPosts'",
        )
        assert post["content"] == "An old post\non two lines"
        assert post["author"] == ME and post["is_own_content"] == 1
        assert post["created_at"].startswith("2024-03-30 17:02:00+00:00")

        comments = _items(
            conn, "json_extract(metadata, '$.source') = 'export' AND kind = 'comment'"
        )
        assert [c["content"] for c in comments] == [
            'Says "hello", twice',
            "Another in the same second",
        ]
        assert comments[0]["source_id"] != comments[1]["source_id"]
        # Their post isn't held: each replies to the post's stub
        (stub,) = _items(conn, "kind = 'stub'")
        links = conn.execute(
            "SELECT from_id FROM item_links WHERE to_id = ? AND kind = 'in_reply_to'",
            (stub["id"],),
        ).fetchall()
        assert {r[0] for r in links} == {c["id"] for c in comments}

        # The comment the sync held without text now has the export's
        assert _items(conn, "source_id = 'changelog-socialActions/comments-1'")[0]["content"] == (
            "Text the sync never gave"
        )


def test_import_twice_adds_nothing(config: Config, export: Path) -> None:
    with get_connection(config) as conn:
        _held(conn)
        import_export(conn, export)
        count = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        again = import_export(conn, export)
        assert (again.posts_added, again.comments_added, again.articles_added) == (0, 0, 0)
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == count


def test_an_article_held_by_hand_gets_the_full_text(config: Config, export: Path) -> None:
    with get_connection(config) as conn:
        upsert_item(
            conn,
            ItemCreate(
                source_type="linkedin",
                source_id="https://www.linkedin.com/pulse/an-article-me-abc/",
                title="Added by hand",
                content="An excerpt",
                metadata={"resource_name": "article", "captured_from": "notes"},
            ),
        )
        report = import_export(conn, export)
        assert (report.articles_added, report.articles_updated) == (1, 1)
        (article,) = _items(conn, "source_id LIKE 'https://www.linkedin.com/pulse/%'")
        assert article["title"] == "An article"
        assert "Second, with a [link](https://x.org)." in article["content"]
        assert '"captured_from": "notes"' in article["metadata"]


def test_parse_article_reads_times_and_drafts() -> None:
    article = parse_article(ARTICLE, "an-article-me-abc.html")
    assert article is not None
    assert article.url == "https://www.linkedin.com/pulse/an-article-me-abc"
    assert article.published is not None and article.published.isoformat().startswith(
        "2024-02-07T21:03"
    )
    assert article.text.startswith("First paragraph.")
    assert "## Part" in article.text and "Created on" not in article.text

    draft = parse_article(DRAFT, "2021-10-21 03:23:26.0-A draft.html")
    assert draft is not None and draft.published is None and draft.url is None
    assert draft.title == "A draft"


def test_not_an_export(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "other.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with get_connection(config) as conn, pytest.raises(ValueError, match="Not a LinkedIn"):
        import_export(conn, path)


def _zip(tmp_path: Path, shares: str = "", comments: str = "") -> Path:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Shares.csv", shares or "Date,ShareLink,ShareCommentary\n")
        zf.writestr("Comments.csv", comments or "Date,Link,Message\n")
    return path


def _sync_comment(conn, n: int, text: str, on: str, captured: int, actor: str = ME) -> None:
    upsert_item(
        conn,
        ItemCreate(
            source_type="linkedin",
            source_id=f"changelog-socialActions/comments-{n}",
            content=text,
            is_own_content=True,
            metadata={
                "source": "changelog",
                "resource_name": "socialActions/comments",
                "commented_on": on,
                "raw": {"capturedAt": captured, "actor": actor, "owner": ME},
            },
        ),
    )


def test_a_held_comment_matches_one_export_row_and_text_wins(
    config: Config, tmp_path: Path
) -> None:
    # Two of your comments on one post, a second apart: each matches its own held
    # copy by text, though both are within 5 s of the first
    comments = (
        "Date,Link,Message\n"
        f"2026-01-19 10:45:54,{FEED}urn:li:activity:7418912385886941184,First\n"
        f"2026-01-19 10:45:55,{FEED}urn:li:activity:7418912385886941184,Second\n"
        f"2026-01-19 10:45:56,{FEED}urn:li:activity:7418912385886941184,Third\n"
    )
    with get_connection(config) as conn:
        on = "urn:li:activity:7418912385886941184"
        _sync_comment(conn, 1, "Second", on, 1768819554000)
        _sync_comment(conn, 2, "First", on, 1768819555000)
        report = import_export(conn, _zip(tmp_path, comments=comments))
        # Third is new: the held comments near its time are taken
        assert (report.comments_held, report.comments_added) == (2, 1)


def test_others_comments_on_your_posts_are_not_yours(config: Config, tmp_path: Path) -> None:
    comments = (
        f"Date,Link,Message\n2026-01-19 10:45:54,{FEED}urn:li:activity:7418912385886941184,Mine\n"
    )
    with get_connection(config) as conn:
        on = "urn:li:activity:7418912385886941184"
        _sync_comment(
            conn, 1, "CREATE socialActions/comments", on, 1768819554000, "urn:li:person:x"
        )
        report = import_export(conn, _zip(tmp_path, comments=comments))
        assert (report.comments_held, report.comments_added, report.filled) == (0, 1, 0)


def test_a_comment_held_on_the_share_matches_one_on_its_activity(
    config: Config, tmp_path: Path
) -> None:
    # A share and its activity are one post, made within 2 s of each other
    share = "urn:li:share:7418912385886941184"
    activity_id = (snowflake_ms(share) + 500) << 22
    comments = f"Date,Link,Message\n2026-01-19 10:45:54,{FEED}urn:li:activity:{activity_id},Same\n"
    with get_connection(config) as conn:
        _sync_comment(conn, 1, "Same", share, 1768819554000)
        report = import_export(conn, _zip(tmp_path, comments=comments))
        assert (report.comments_held, report.comments_added) == (1, 0)


def test_posts_and_comments_from_the_snapshot_fetch_are_held(
    config: Config, tmp_path: Path
) -> None:
    link = f"{FEED}urn:li:share:7000000000000000000"
    with get_connection(config) as conn:
        for domain, text in (("MEMBER_SHARE_INFO", "A post"), ("ALL_COMMENTS", "A comment")):
            upsert_item(
                conn,
                ItemCreate(
                    source_type="linkedin",
                    source_id=f"{link}#{domain}",
                    url=link,
                    content=text,
                    is_own_content=True,
                    metadata={"domain": domain, "raw": {}},
                ),
            )
        report = import_export(
            conn,
            _zip(
                tmp_path,
                shares=f"Date,ShareLink,ShareCommentary\n2024-01-01 10:00:00,{link},A post\n",
                comments=f"Date,Link,Message\n2024-01-01 11:00:00,{link},A comment\n",
            ),
        )
        assert (report.posts_held, report.posts_added) == (1, 0)
        assert (report.comments_held, report.comments_added) == (1, 0)


def test_a_later_text_match_is_not_taken_by_an_earlier_time_match(
    config: Config, tmp_path: Path
) -> None:
    # Row 1 has no held copy, but is within 5 s of the held "Exact"; row 2 is "Exact"
    comments = (
        "Date,Link,Message\n"
        f"2026-01-19 10:45:54,{FEED}urn:li:activity:7418912385886941184,New one\n"
        f"2026-01-19 10:46:30,{FEED}urn:li:activity:7418912385886941184,Exact\n"
    )
    with get_connection(config) as conn:
        _sync_comment(conn, 1, "Exact", "urn:li:activity:7418912385886941184", 1768819555000)
        report = import_export(conn, _zip(tmp_path, comments=comments))
        assert (report.comments_held, report.comments_added) == (1, 1)
        added = _items(conn, "json_extract(metadata, '$.source') = 'export'")
        assert [a["content"] for a in added] == ["New one"]


def test_comments_on_a_snapshot_post_go_under_it(config: Config, tmp_path: Path) -> None:
    link = f"{FEED}urn:li:share:7000000000000000000"
    with get_connection(config) as conn:
        post_id = upsert_item(
            conn,
            ItemCreate(
                source_type="linkedin",
                source_id=link,
                url=link,
                content="A post",
                is_own_content=True,
                metadata={"domain": "MEMBER_SHARE_INFO", "raw": {}},
            ),
        )
        import_export(
            conn,
            _zip(
                tmp_path,
                shares=f"Date,ShareLink,ShareCommentary\n2024-01-01 10:00:00,{link},A post\n",
                comments=f"Date,Link,Message\n2024-01-01 11:00:00,{link},On it\n",
            ),
        )
        (comment,) = _items(conn, "kind = 'comment'")
        assert comment["parent_id"] == post_id


def test_parse_article_keeps_its_own_headings() -> None:
    page = ARTICLE.replace("<h2>Part</h2>", "<h1>Section one</h1>")
    article = parse_article(page, "an-article-me-abc.html")
    assert article is not None and article.title == "An article"
    assert "# Section one" in article.text


def test_repeated_and_emoji_replies_match_by_time(config: Config, tmp_path: Path) -> None:
    on = "urn:li:activity:7418912385886941184"
    comments = (
        "Date,Link,Message\n"
        f"2026-01-19 10:00:00,{FEED}{on},Thanks!\n"
        f"2026-01-19 11:00:00,{FEED}{on},Thanks!\n"
        f"2026-01-19 12:00:00,{FEED}{on},👍\n"
        f"2026-01-19 13:00:00,{FEED}{on},🙏\n"
    )
    hour = 3600 * 1000
    t0 = 1768816800000  # 2026-01-19 10:00:00 UTC
    with get_connection(config) as conn:
        # Held in the other order: each must match the row made at its time
        _sync_comment(conn, 1, "Thanks!", on, t0 + hour + 400)
        _sync_comment(conn, 2, "Thanks!", on, t0 + 400)
        _sync_comment(conn, 3, "🙏", on, t0 + 3 * hour + 400)
        report = import_export(conn, _zip(tmp_path, comments=comments))
        # The 👍 at 12:00 isn't held; the 🙏 isn't taken for it by its empty text
        assert (report.comments_held, report.comments_added) == (3, 1)
        (added,) = _items(conn, "json_extract(metadata, '$.source') = 'export'")
        assert added["content"] == "👍"


def test_an_emoji_comment_from_the_snapshot_fetch_is_held(config: Config, tmp_path: Path) -> None:
    link = f"{FEED}urn:li:activity:7100000000000000000"
    with get_connection(config) as conn:
        upsert_item(
            conn,
            ItemCreate(
                source_type="linkedin",
                source_id=link,
                url=link,
                content="👍",
                is_own_content=True,
                metadata={"domain": "ALL_COMMENTS", "raw": {}},
            ),
        )
        comments = (
            f"Date,Link,Message\n2024-01-01 11:00:00,{link},👍\n2024-01-01 12:00:00,{link},🙏\n"
        )
        report = import_export(conn, _zip(tmp_path, comments=comments))
        assert (report.comments_held, report.comments_added) == (1, 1)


# As Shares.csv writes a post of several lines: each line in its own quotes
QUOTED_LINES = 'Testing out Bluesky 🤓"\n""\n"It started on the "Eurostar". "\n""\n"#Bayesian '


def test_share_text_takes_off_the_quotes_round_each_line() -> None:
    assert (
        share_text(QUOTED_LINES)
        == 'Testing out Bluesky 🤓\n\nIt started on the "Eurostar". \n\n#Bayesian'
    )
    assert share_text('One line with a "quote"') == 'One line with a "quote"'
    # Not in the export's form: left as it is
    assert share_text('Line one\n"Quoted line"') == 'Line one\n"Quoted line"'


def test_a_post_imported_with_quotes_is_put_right(config: Config, tmp_path: Path) -> None:
    link = f"{FEED}urn:li:share:7000000000000000000"
    field = QUOTED_LINES.replace('"', '""')
    shares = f'Date,ShareLink,ShareCommentary\n2024-12-04 10:00:00,{link},"{field}"\n'
    with get_connection(config) as conn:
        # As the import before this fix stored it
        upsert_item(
            conn,
            ItemCreate(
                source_type="linkedin",
                source_id="export-ugcPosts-urn:li:share:7000000000000000000",
                content=QUOTED_LINES.strip(),
                metadata={
                    "source": "export",
                    "resource_name": "ugcPosts",
                    "post_id": "urn:li:share:7000000000000000000",
                },
            ),
        )
        import_export(conn, _zip(tmp_path, shares=shares))
        (post,) = _items(conn, "kind = 'post'")
        assert post["content"].startswith("Testing out Bluesky 🤓\n\nIt started")
