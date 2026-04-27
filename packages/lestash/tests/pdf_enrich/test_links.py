from lestash.core.pdf_enrich.links import Link, apply_links


def _link(uri: str, anchor: str) -> Link:
    return Link(page=0, bbox=(0, 0, 0, 0), uri=uri, anchor_text=anchor)


def test_replaces_first_occurrence_only():
    md = "click here and click here again"
    links = [_link("https://a.example", "click here")]
    out = apply_links(md, links)
    assert out == "[click here](https://a.example) and click here again"


def test_two_links_same_anchor_walk_left_to_right():
    md = "see here and then see here at the bottom"
    links = [
        _link("https://1.example", "see here"),
        _link("https://2.example", "see here"),
    ]
    out = apply_links(md, links)
    assert (
        out == "[see here](https://1.example) and then [see here](https://2.example) at the bottom"
    )


def test_normalised_match_across_line_breaks():
    # Docling reflowed the anchor — newline + extra whitespace
    md = "Visit the\n  Dijkstra Archive  for the source."
    links = [_link("https://cs.utexas.edu/EWD", "Dijkstra Archive")]
    out = apply_links(md, links)
    assert "[Dijkstra Archive" in out
    assert "(https://cs.utexas.edu/EWD)" in out


def test_unmatched_link_is_appended_not_dropped():
    md = "no anchor here"
    links = [_link("https://lost.example", "ghost text")]
    out = apply_links(md, links)
    assert "no anchor here" in out
    assert "<!-- unmatched-links -->" in out
    assert "https://lost.example" in out


def test_empty_anchor_text_is_unmatched():
    md = "hello world"
    links = [_link("https://empty.example", "")]
    out = apply_links(md, links)
    assert "<!-- unmatched-links -->" in out
    assert "https://empty.example" in out


def test_no_links_returns_markdown_unchanged():
    md = "## Heading\n\nbody text"
    assert apply_links(md, []) == md


def test_soft_hyphen_is_ignored_in_match():
    md = "the Dij­kstra Archive"
    links = [_link("https://x", "Dijkstra Archive")]
    out = apply_links(md, links)
    assert "(https://x)" in out


# --- Aggressive fallback (regression for #143) -------------------------------


def test_aggressive_match_recovers_ampersand_anchor():
    """Anchor 'Barnes & Noble' should match Docling's reformatted output
    where the ampersand was rendered or escaped differently."""
    md = "available at Barnes  Noble (online)"
    links = [_link("https://barnes-noble.example", "Barnes & Noble")]
    out = apply_links(md, links)
    assert "(https://barnes-noble.example)" in out
    assert "<!-- unmatched-links -->" not in out


def test_aggressive_match_recovers_doi_with_punctuation():
    """A DOI anchor 'DOI: 10.1007/11568285_9' should match a stripped form."""
    md = "see DOI 10 1007 11568285 9 in the references"
    links = [_link("https://doi.example/x", "DOI: 10.1007/11568285_9")]
    out = apply_links(md, links)
    assert "(https://doi.example/x)" in out


def test_aggressive_match_handles_smart_quotes():
    """O’Reilly (curly apostrophe) → O'Reilly via NFKC."""
    md = "the O’Reilly handbook"
    links = [_link("https://oreilly.example", "O'Reilly")]
    out = apply_links(md, links)
    assert "(https://oreilly.example)" in out


def test_strict_match_still_wins_when_both_pass():
    """If a strict match exists, the strict span (not the aggressive one) is
    used so we don't accidentally widen the rewritten anchor."""
    md = "click here for docs"
    links = [_link("https://x", "click here")]
    out = apply_links(md, links)
    assert out == "[click here](https://x) for docs"
