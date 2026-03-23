"""Parse a JSON array of items for import."""

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

        results.append(
            ItemCreate(
                source_type=item["source_type"],
                source_id=item.get("source_id"),
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
