"""Run the search baseline queries against a LeStash server and save the results (#215).

Each query in docs/search-baseline/queries.toml runs through GET /api/items/search in
keyword, semantic and hybrid modes. The top results (item id, source key, title, rank)
are saved as JSON, and a summary prints where each expected item ranked.

    uv run python scripts/search_baseline.py --url https://pop-mini.monkey-ladon.ts.net:8444
    uv run python scripts/search_baseline.py --url ... --out docs/search-baseline/results.json

Read-only: it only calls the search endpoint.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import tomllib
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MODES = ("keyword", "semantic", "hybrid")
DEFAULT_QUERIES = Path(__file__).resolve().parent.parent / "docs/search-baseline/queries.toml"


KINDS = ("title", "topic", "paraphrase", "deep")
FIELDS = {"q", "kind", "expect"}
# Anything but letters, digits and spaces. The first recording ran before #227, when
# punctuation broke keyword search; queries stay words-only so runs compare like with like
_FTS_BREAKERS = re.compile(r"[^\w\s]")


def load_queries(path: Path) -> list[dict[str, Any]]:
    """The queries, each with q, a known kind and a non-empty expect list."""
    queries = tomllib.loads(path.read_text())["query"]
    for query in queries:
        expect = query.get("expect")
        problems = [
            "no q" if not query.get("q") else "",
            f"kind not one of {', '.join(KINDS)}" if query.get("kind") not in KINDS else "",
            "expect must list source_type:source_id keys"
            if not (isinstance(expect, list) and expect and all(":" in k for k in expect))
            else "",
            f"unknown field(s) {sorted(set(query) - FIELDS)}" if set(query) - FIELDS else "",
            # Words only, as in the first recording
            "q must be words only: letters, digits and spaces"
            if _FTS_BREAKERS.search(str(query.get("q", "")))
            else "",
        ]
        if any(problems):
            raise SystemExit(f"{path}: {'; '.join(p for p in problems if p)}: {query}")
    return queries


def search(base_url: str, q: str, mode: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    """The top results for one query and mode; raises for an HTTP or network error."""
    params = urllib.parse.urlencode({"q": q, "mode": mode, "limit": limit})
    url = f"{base_url.rstrip('/')}/api/items/search?{params}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (a given URL)
        items = json.load(resp)["items"]
    return [
        {
            "rank": rank,
            "id": item["id"],
            "key": f"{item['source_type']}:{item.get('source_id')}",
            "title": (item.get("title") or "")[:120],
        }
        for rank, item in enumerate(items, start=1)
    ]


def run(base_url: str, queries: list[dict[str, Any]], limit: int, timeout: float) -> dict:
    results = []
    for query in queries:
        entry: dict[str, Any] = {
            "q": query["q"],
            "kind": query.get("kind"),
            "expect": query.get("expect", []),
        }
        for mode in MODES:
            try:
                entry[mode] = search(base_url, query["q"], mode, limit, timeout)
            # OSError covers URLError, timeouts and dropped connections; HTTPException
            # covers IncompleteRead; the rest, a response of an unexpected shape. One
            # failed cell must not lose the whole recording.
            except (OSError, http.client.HTTPException, KeyError, ValueError, TypeError) as e:
                entry[mode] = {"error": str(e)}
        # Hybrid mode swallows a failure of either half and returns the other half's
        # results, saying so only in the server log. A failed single mode shows it.
        entry["hybrid_partial"] = [m for m in ("keyword", "semantic") if _failed(entry[m])]
        results.append(entry)
    vector_ok = all(not _failed(e["semantic"]) for e in results)
    return {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "server": base_url,
        "limit": limit,
        "vector_search": vector_ok,
        "results": results,
    }


def _failed(cell: object) -> bool:
    return isinstance(cell, dict)


def expected_ranks(entry: dict[str, Any], mode: str) -> str:
    """'1, -' style: each expected item's rank in this mode, '-' when not in the top results."""
    hits = entry[mode]
    if isinstance(hits, dict):
        return "error"
    ranks = {hit["key"]: hit["rank"] for hit in hits}
    return ", ".join(str(ranks.get(key, "-")) for key in entry["expect"]) or "(no expect)"


def summary(report: dict) -> str:
    lines = [f"{'kind':<11} {'keyword':>9} {'semantic':>9} {'hybrid':>9}  query"]
    for entry in report["results"]:
        cells = [f"{expected_ranks(entry, mode):>9}" for mode in MODES]
        lines.append(f"{entry['kind'] or '':<11} {' '.join(cells)}  {entry['q'][:60]}")
        if entry["hybrid_partial"]:
            failed = " and ".join(entry["hybrid_partial"])
            lines.append(f"{'':<11} hybrid above ran without {failed} search")
    if not report["vector_search"]:
        lines.append("WARNING: semantic search failed, so hybrid results are keyword-only.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="LeStash server, e.g. https://host:8444")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--out", type=Path, help="Write the JSON results here")
    parser.add_argument("--limit", type=int, default=10, help="Results per query (1-100)")
    parser.add_argument("--timeout", type=float, default=60.0, help="Seconds per request")
    args = parser.parse_args(argv)

    report = run(args.url, load_queries(args.queries), args.limit, args.timeout)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(f"Wrote {args.out}", file=sys.stderr)
    print(summary(report))
    errors = sum(isinstance(e[m], dict) for e in report["results"] for m in MODES)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
