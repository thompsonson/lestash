from lestash.core.pdf_enrich.images import apply_images, count_placeholders


def test_count_placeholders():
    assert count_placeholders("") == 0
    assert count_placeholders("<!-- image -->") == 1
    assert count_placeholders("a <!-- image --> b <!-- image --> c") == 2
    # Case-insensitive
    assert count_placeholders("<!-- IMAGE -->") == 1


def test_apply_images_replaces_in_order():
    md = "first <!-- image --> middle <!-- image --> last"
    out = apply_images(
        md,
        {0: "![a](/api/media/1)", 1: "![b](/api/media/2)"},
    )
    assert out == "first ![a](/api/media/1) middle ![b](/api/media/2) last"


def test_apply_images_leaves_unmatched_placeholders_intact():
    md = "<!-- image --> A <!-- image --> B"
    out = apply_images(md, {0: "![only](/api/media/9)"})
    assert out == "![only](/api/media/9) A <!-- image --> B"


def test_apply_images_no_placeholders_is_noop():
    md = "no images here"
    assert apply_images(md, {}) == md
