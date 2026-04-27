from lestash.core.pdf_enrich.cleanup import strip_artifacts


def test_strips_trailing_interpunct():
    assert strip_artifacts("foo bar ·\n") == "foo bar\n"
    assert strip_artifacts("foo bar ◦\n") == "foo bar\n"


def test_preserves_inline_interpunct():
    assert strip_artifacts("m·s⁻¹") == "m·s⁻¹"
    assert strip_artifacts("a·b·c\n") == "a·b·c\n"


def test_strips_only_at_end_of_line():
    src = "first ·\nsecond ◦\nthird line\n"
    assert strip_artifacts(src) == "first\nsecond\nthird line\n"


def test_leaves_normal_content_intact():
    src = "## Heading\n\n- list item\n- another\n"
    assert strip_artifacts(src) == src
