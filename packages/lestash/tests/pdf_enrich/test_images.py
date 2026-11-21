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


def test_extract_images_emits_one_entry_per_page_occurrence(make_pdf, red_dot_png):
    """Regression for #143: same image used on multiple pages must produce
    one ExtractedImage per page-occurrence (so each Docling placeholder gets
    a replacement) but the entries share an xref_hash so persistence dedups."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_images

    pdf = make_pdf(
        [
            {"images": [((100, 100, 200, 200), red_dot_png)]},
            {"images": [((100, 100, 200, 200), red_dot_png)]},
            {"images": [((100, 100, 200, 200), red_dot_png)]},
        ]
    )
    doc = pymupdf.open(pdf)
    try:
        images = extract_images(doc)
    finally:
        doc.close()

    assert len(images) == 3
    assert {img.xref_hash for img in images} == {images[0].xref_hash}
    assert [img.placeholder_index for img in images] == [0, 1, 2]
    assert [img.page for img in images] == [0, 1, 2]


def _diagram_pdf(tmp_path):
    """A page with a box and an arrow drawn as vector shapes, and no raster image."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(pymupdf.Rect(100, 100, 300, 200), color=(0, 0, 1), fill=(0.9, 0.9, 1))
    page.draw_line(pymupdf.Point(200, 200), pymupdf.Point(200, 300), color=(0, 0, 0))
    out = tmp_path / "diagram.pdf"
    doc.save(out)
    doc.close()
    return out


def test_extract_pictures_renders_a_diagram_drawn_as_shapes(tmp_path):
    """#391: a diagram with no raster image is rendered from its region."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open(_diagram_pdf(tmp_path))
    try:
        images = extract_pictures(doc, [PictureRegion(0, (90.0, 90.0, 310.0, 310.0))])
    finally:
        doc.close()

    assert len(images) == 1
    image = images[0]
    assert (image.placeholder_index, image.page, image.mime_type) == (0, 0, "image/png")
    rendered = pymupdf.Pixmap(image.bytes_)
    assert (rendered.width, rendered.height) == (440, 440)  # 220 points at 144 dpi
    assert image.bbox == (90.0, 90.0, 310.0, 310.0)


def test_extract_pictures_keeps_a_raster_image_as_stored(make_pdf, red_dot_png):
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    pdf = make_pdf([{"images": [((100, 100, 200, 200), red_dot_png)]}])
    doc = pymupdf.open(pdf)
    try:
        stored = doc.extract_image(doc[0].get_images()[0][0])["image"]
        images = extract_pictures(doc, [PictureRegion(0, (98.0, 98.0, 202.0, 202.0))])
    finally:
        doc.close()

    assert [img.bytes_ for img in images] == [stored]


def test_extract_pictures_keeps_each_picture_on_its_own_placeholder(tmp_path):
    """A picture with no position is skipped; the next keeps its own index."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open(_diagram_pdf(tmp_path))
    try:
        images = extract_pictures(
            doc,
            [None, PictureRegion(5, (0, 0, 10, 10)), PictureRegion(0, (90, 90, 310, 310))],
        )
    finally:
        doc.close()

    assert [img.placeholder_index for img in images] == [2]


def test_extract_pictures_renders_a_diagram_drawn_over_a_page_background(tmp_path, red_dot_png):
    """A raster image much larger than the picture is not the picture."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=red_dot_png)
    page.draw_rect(pymupdf.Rect(100, 100, 300, 200), fill=(0, 0, 1))
    try:
        background = doc.extract_image(page.get_images()[0][0])["image"]
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 300.0, 200.0))])
    finally:
        doc.close()

    assert len(images) == 1
    assert images[0].bytes_ != background
    assert images[0].mime_type == "image/png"


def test_regions_fit_needs_a_usable_region_for_every_placeholder(tmp_path):
    import pymupdf
    from lestash.core.pdf_enrich.images import regions_fit
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open(_diagram_pdf(tmp_path))
    good = PictureRegion(0, (0, 0, 10, 10))
    try:
        assert regions_fit(doc, [good], 1)
        assert not regions_fit(doc, [good], 2)
        assert not regions_fit(doc, [], 0)
        assert not regions_fit(doc, [good, None], 2)
        assert not regions_fit(doc, [good, PictureRegion(3, (0, 0, 10, 10))], 2)
        assert not regions_fit(doc, [good, PictureRegion(0, (700, 900, 800, 1000))], 2)
    finally:
        doc.close()


def test_extract_pictures_renders_a_photo_with_labels_drawn_over_it(make_pdf, red_dot_png):
    """A stored photo would lose the arrow drawn over it, so the region is rendered."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    pdf = make_pdf([{"images": [((100, 100, 200, 200), red_dot_png)]}])
    doc = pymupdf.open(pdf)
    try:
        doc[0].draw_line(pymupdf.Point(110, 150), pymupdf.Point(190, 150), color=(0, 0, 0))
        stored = doc.extract_image(doc[0].get_images()[0][0])["image"]
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 200.0, 200.0))])
    finally:
        doc.close()

    assert len(images) == 1
    assert images[0].bytes_ != stored
