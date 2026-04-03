"""Parse a JSON array of items for import."""

import hashlib
import json

from lestash.models.item import ItemCreate


def parse_json_items(data: bytes) -> list[ItemCreate]:
    """Parse a JSON array into ItemCreate objects.

    Expects a JSON array where each element has at least
    'source_type' and 'content' fields.

    Args:
        data: Raw JSON bytes.

    Returns:
        List of ItemCreate objects.

    Raises:
        ValueError: If the JSON is not a valid array of items.
    """
    try:
        items = json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e

    if not isinstance(items, list):
        raise ValueError("Expected a JSON array of items")

    results = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Item {i} is not an object")
        if "content" not in item:
            raise ValueError(f"Item {i} missing required field 'content'")
        if "source_type" not in item:
            item["source_type"] = "import"

        # Generate deterministic source_id from content if missing
        source_id = item.get("source_id")
        if not source_id and not item.get("url"):
            content_hash = hashlib.sha256(item["content"][:500].encode()).hexdigest()[:12]
            source_id = f"{item['source_type']}-{content_hash}"

        results.append(
            ItemCreate(
                source_type=item["source_type"],
                source_id=source_id,
                url=item.get("url"),
                title=item.get("title"),
                content=item["content"],
                author=item.get("author"),
                created_at=item.get("created_at"),
                is_own_content=item.get("is_own_content", True),
                metadata=item.get("metadata"),
            )
        )

    return results
