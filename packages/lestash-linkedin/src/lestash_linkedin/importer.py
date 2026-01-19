"""LinkedIn data export importer."""

import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from lestash.models.item import ItemCreate


def parse_linkedin_date(date_str: str) -> datetime | None:
    """Parse LinkedIn export date format."""
    if not date_str:
        return None

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    return None


def import_posts_from_zip(zip_path: Path) -> Iterator[ItemCreate]:
    """Import posts from LinkedIn data export ZIP.

    LinkedIn data exports contain a Posts.csv or similar file
    with your post content.

    Args:
        zip_path: Path to the LinkedIn export ZIP file.

    Yields:
        ItemCreate for each post found.
    """
    import csv
    import io

    with zipfile.ZipFile(zip_path, "r") as zf:
        # Find posts file (could be in different locations)
        posts_files = [
            name for name in zf.namelist() if "post" in name.lower() and name.endswith(".csv")
        ]

        if not posts_files:
            # Try to find shares file
            posts_files = [
                name for name in zf.namelist() if "share" in name.lower() and name.endswith(".csv")
            ]

        if not posts_files:
            raise ValueError("No posts/shares CSV found in LinkedIn export")

        for posts_file in posts_files:
            with zf.open(posts_file) as f:
                content = f.read().decode("utf-8")
                reader = csv.DictReader(io.StringIO(content))

                for row in reader:
                    # LinkedIn exports vary, try common field names
                    post_content = (
                        row.get("ShareCommentary")
                        or row.get("Commentary")
                        or row.get("Content")
                        or row.get("Text")
                        or ""
                    )

                    if not post_content.strip():
                        continue

                    date_str = row.get("Date") or row.get("SharedDate") or row.get("Created") or ""

                    url = row.get("ShareLink") or row.get("Link") or row.get("URL")

                    yield ItemCreate(
                        source_type="linkedin",
                        source_id=url or f"post-{hash(post_content)}",
                        url=url,
                        content=post_content.strip(),
                        created_at=parse_linkedin_date(date_str),
                        is_own_content=True,
                        metadata={
                            "import_file": posts_file,
                            "raw_row": dict(row),
                        },
                    )


def import_from_zip(zip_path: Path) -> Iterator[ItemCreate]:
    """Import all content from LinkedIn data export ZIP.

    Args:
        zip_path: Path to the LinkedIn export ZIP file.

    Yields:
        ItemCreate for each item found.
    """
    yield from import_posts_from_zip(zip_path)
