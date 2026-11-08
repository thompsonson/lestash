#!/usr/bin/env bash
# Ad hoc, read-only check of the LeStash API: core GET endpoints, then each baseline
# search query in keyword, semantic and hybrid modes, with its URL and top hits.
#
#   scripts/adhoc/api-check.sh [BASE_URL] [QUERIES_TOML]
#
# Needs curl, jq and python3. QUERIES_TOML defaults to docs/search-baseline/queries.toml.
#
# Each response must be HTTP 200, valid JSON, and the expected shape. Exit 1 otherwise.
set -uo pipefail

BASE="${1:-https://pop-mini.monkey-ladon.ts.net:8444}"
QUERIES="${2:-$(cd "$(dirname "$0")/../.." && pwd)/docs/search-baseline/queries.toml}"
TOP=3
fail=0

# An item has a numeric id and a source_type; a page holds items plus a numeric total
ITEM='(type == "object" and (.id | type == "number") and (.source_type | type == "string"))'
PAGE="(.items | type == \"array\") and (.total | type == \"number\") and all(.items[]; $ITEM)"

# GET a path and check the response: HTTP 200, a JSON body, and the jq expression CHECK
# true on it. Print status, time and URL; leave the body in $BODY.
get() {
    local path="$1" check="${2:-true}" out code time why=""
    out=$(curl -s -m 30 -w '\n%{http_code} %{time_total}' "$BASE$path") || { echo "FAIL  curl error  $BASE$path"; fail=1; BODY=""; return 1; }
    code=$(tail -n1 <<<"$out" | cut -d' ' -f1)
    time=$(tail -n1 <<<"$out" | cut -d' ' -f2)
    BODY=$(sed '$d' <<<"$out")
    if [ "$code" != 200 ]; then
        why="HTTP $code"
    elif ! jq -e . >/dev/null 2>&1 <<<"$BODY"; then
        why="not JSON"
    elif [ "$(jq -r "$check" 2>&1 <<<"$BODY")" != true ]; then
        why="unexpected shape: $check"
    fi
    if [ -z "$why" ]; then
        printf 'ok    %s  %5.2fs  %s\n' "$code" "$time" "$BASE$path"
    else
        printf 'FAIL  %s  %5.2fs  %s  (%s)\n' "$code" "$time" "$BASE$path" "$why"
        fail=1
        return 1
    fi
}

urlencode() { jq -rn --arg s "$1" '$s|@uri'; }

echo "== API check of $BASE at $(date '+%a %-d %b %Y, %H:%M %Z')"
echo
echo "-- Core endpoints"
get /api/health '.status == "ok" and (.items | type == "number") and .items > 0'
get /api/stats '(.total_items | type == "number") and has("sources")'
get /api/sources 'type == "array" and length > 0 and all(.[]; (.name | type == "string") and has("enabled"))'
get /api/sources/status 'type == "array" and all(.[]; (.source_type | type == "string") and (.status | type == "string"))'
get /api/embeddings/status '(.embedded | type == "number") and (.dimensions | type == "number")'
get "/api/items?limit=5" "$PAGE and (.items | length) > 0"
get /api/items/tags '.tags | type == "array"'
get /api/collections 'type == "array" and all(.[]; (.id | type == "number") and (.name | type == "string"))'

# One item's detail, children and similar items, taken from the newest top-level item
if get "/api/items?limit=1" "$PAGE"; then
    id=$(jq -r '(.items // .)[0].id // empty' <<<"$BODY")
    if [ -n "$id" ]; then
        get "/api/items/$id" "$ITEM and .id == $id and has(\"content\") and (.media | type == \"array\")"
        get "/api/items?parent_id=$id&limit=5" "$PAGE and all(.items[]; .parent_id == $id)"
        get "/api/embeddings/similar/$id" "type == \"array\" and all(.[]; $ITEM and .id != $id)"
    fi
fi

echo
echo "-- Search queries from $QUERIES"
if [ ! -f "$QUERIES" ]; then
    echo "FAIL  no queries file (it arrives with PR #224)"
    exit 1
fi
# q <TAB> expect, one query per line
mapfile -t rows < <(python3 -I -c '
import sys, tomllib
for q in tomllib.load(open(sys.argv[1], "rb"))["query"]:
    print(q["q"] + "\t" + " ".join(q.get("expect", [])))
' "$QUERIES")

for row in "${rows[@]}"; do
    q="${row%%$'\t'*}"
    expect="${row#*$'\t'}"
    echo
    echo "\"$q\"   expect: $expect"
    for mode in keyword semantic hybrid; do
        if get "/api/items/search?q=$(urlencode "$q")&mode=$mode&limit=10" "$PAGE and (.items | length) <= 10"; then
            # Top hits, and the rank of each expected item ("source_type:source_id")
            jq -r --argjson top "$TOP" --arg expect "$expect" '
                (.items) as $r
                | ($r[:$top][] | "        \(.source_type): \(.title // "(no title)" | .[0:80])"),
                  ($expect | split(" ")[] | select(length > 0) as $e
                   | ([$r | to_entries[] | select("\(.value.source_type):\(.value.source_id)" == $e) | .key + 1][0]) as $rank
                   | "        expected \($e): " + (if $rank then "rank \($rank)" else "not in top 10" end))
            ' <<<"$BODY"
        fi
    done
done

echo
if [ "$fail" = 0 ]; then echo "== All requests returned 200"; else echo "== Some requests failed"; fi
exit "$fail"
