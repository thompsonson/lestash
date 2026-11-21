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


def _photo_jpeg(size: int) -> bytes:
    """A photo as a camera would store it: JPEG data."""
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, size, size), False)
    pix.set_rect(pix.irect, (200, 30, 30))
    return pix.tobytes("jpeg")


def test_extract_pictures_renders_a_photo_at_its_own_resolution(make_pdf):
    """A 400-pixel photo in a 100-point box is 288 dpi; the picture keeps that."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    pdf = make_pdf([{"images": [((100, 100, 200, 200), _photo_jpeg(400))]}])
    doc = pymupdf.open(pdf)
    try:
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 200.0, 200.0))])
    finally:
        doc.close()

    assert len(images) == 1
    assert images[0].mime_type == "image/jpeg"
    assert pymupdf.Pixmap(images[0].bytes_).width == 400


def test_extract_pictures_caps_the_resolution(make_pdf):
    import pymupdf
    from lestash.core.pdf_enrich.images import MAX_RENDER_DPI, extract_pictures
    from lestash.core.text_extract import PictureRegion

    pdf = make_pdf([{"images": [((100, 100, 110, 110), _photo_jpeg(400))]}])
    doc = pymupdf.open(pdf)
    try:
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 110.0, 110.0))])
    finally:
        doc.close()

    expected = round(10 * MAX_RENDER_DPI / 72)
    assert abs(pymupdf.Pixmap(images[0].bytes_).width - expected) <= 1


def test_extract_pictures_keeps_what_is_drawn_over_a_photo(make_pdf):
    """An arrow drawn over a photo is in the picture, which a stored photo would lose."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    pdf = make_pdf([{"images": [((100, 100, 200, 200), _photo_jpeg(100))]}])
    doc = pymupdf.open(pdf)
    region = [PictureRegion(0, (100.0, 100.0, 200.0, 200.0))]
    try:
        plain = extract_pictures(doc, region)[0].bytes_
        doc[0].draw_line(pymupdf.Point(110, 150), pymupdf.Point(190, 150), width=4)
        drawn = extract_pictures(doc, region)[0].bytes_
    finally:
        doc.close()

    assert plain != drawn


def test_a_page_background_or_small_icon_leaves_a_diagram_a_png(tmp_path):
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=_photo_jpeg(600))  # background
    page.insert_image(pymupdf.Rect(105, 105, 115, 115), stream=_photo_jpeg(400))  # icon
    page.draw_rect(pymupdf.Rect(100, 100, 300, 300), color=(0, 0, 1))
    try:
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 300.0, 300.0))])
    finally:
        doc.close()

    assert images[0].mime_type == "image/png"
    assert pymupdf.Pixmap(images[0].bytes_).width == 400  # 200 points at 144 dpi


def test_a_rotated_photo_keeps_its_resolution(tmp_path):
    """A 400 x 200 photo turned 90° into a 50 x 100 point box is 288 dpi."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 200), False)
    pix.set_rect(pix.irect, (30, 200, 30))
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(pymupdf.Rect(100, 100, 150, 200), stream=pix.tobytes("jpeg"), rotate=90)
    try:
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 150.0, 200.0))])
    finally:
        doc.close()

    assert images[0].mime_type == "image/jpeg"
    assert pymupdf.Pixmap(images[0].bytes_).height == 400  # 100 points at 288 dpi


def test_a_photo_running_off_the_page_is_measured_by_its_part_on_the_page(tmp_path):
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    # 100 points wide, 70 of them past the right edge; the region holds the 30 that show
    page.insert_image(pymupdf.Rect(582, 100, 682, 200), stream=_photo_jpeg(400))
    try:
        images = extract_pictures(doc, [PictureRegion(0, (580.0, 100.0, 612.0, 200.0))])
    finally:
        doc.close()

    assert images[0].mime_type == "image/jpeg"


def test_an_unevenly_scaled_photo_takes_its_finer_axis(tmp_path):
    """400 x 400 pixels squeezed into 100 x 200 points: 288 dpi across, 144 down."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(
        pymupdf.Rect(100, 100, 200, 300), stream=_photo_jpeg(400), keep_proportion=False
    )
    try:
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 200.0, 300.0))])
    finally:
        doc.close()

    assert pymupdf.Pixmap(images[0].bytes_).width == 400  # 100 points at 288 dpi


def test_an_inline_image_counts(tmp_path):
    """An image written into the page's content (BI … ID … EI) has no xref, but counts."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(pymupdf.Rect(0, 0, 1, 1))  # gives the page a content stream
    pixels = bytes([200, 30, 30]) * (400 * 400)
    # 400 pixels across 100 points, at (100, 100)-(200, 200) from the top-left: 288 dpi
    inline = (
        b"\nq 100 0 0 100 100 592 cm BI /W 400 /H 400 /CS /RGB /BPC 8 ID " + pixels + b"\nEI Q\n"
    )
    xref = page.get_contents()[0]
    doc.update_stream(xref, doc.xref_stream(xref) + inline)
    try:
        assert doc[0].get_images() == []
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 200.0, 200.0))])
    finally:
        doc.close()

    assert images[0].mime_type == "image/png"
    assert pymupdf.Pixmap(images[0].bytes_).width == 400


def test_black_and_white_line_art_stays_a_png_at_its_own_resolution(tmp_path):
    """A 1-bit image is line art: PNG, but at its 288 dpi."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(pymupdf.Rect(0, 0, 1, 1))  # gives the page a content stream
    bits = bytes([0b10101010]) * (400 * 400 // 8)
    inline = b"\nq 100 0 0 100 100 592 cm BI /W 400 /H 400 /CS /G /BPC 1 ID " + bits + b"\nEI Q\n"
    xref = page.get_contents()[0]
    doc.update_stream(xref, doc.xref_stream(xref) + inline)
    try:
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 200.0, 200.0))])
    finally:
        doc.close()

    assert images[0].mime_type == "image/png"
    assert pymupdf.Pixmap(images[0].bytes_).width == 400


def test_a_screenshot_stored_as_png_stays_a_png(make_pdf):
    """A chart or screenshot pasted as a PNG is not turned into a lossy JPEG."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 400), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    pdf = make_pdf([{"images": [((100, 100, 200, 200), pix.tobytes("png"))]}])
    doc = pymupdf.open(pdf)
    try:
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 200.0, 200.0))])
    finally:
        doc.close()

    assert images[0].mime_type == "image/png"
    assert pymupdf.Pixmap(images[0].bytes_).width == 400


def test_a_small_photo_in_a_diagram_leaves_it_a_png(tmp_path):
    """A JPEG thumbnail filling a fifth of a diagram doesn't make the diagram a JPEG."""
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(pymupdf.Rect(100, 100, 300, 300), color=(0, 0, 1))
    page.insert_image(pymupdf.Rect(110, 110, 200, 200), stream=_photo_jpeg(400))
    try:
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 300.0, 300.0))])
    finally:
        doc.close()

    assert images[0].mime_type == "image/png"


def test_an_inline_jpeg_photo_is_a_jpeg(tmp_path):
    import pymupdf
    from lestash.core.pdf_enrich.images import extract_pictures
    from lestash.core.text_extract import PictureRegion

    jpeg = _photo_jpeg(400)
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(pymupdf.Rect(0, 0, 1, 1))  # gives the page a content stream
    inline = b"\nq 100 0 0 100 100 592 cm BI /W 400 /H 400 /CS /RGB /BPC 8 /F /DCT ID "
    xref = page.get_contents()[0]
    doc.update_stream(xref, doc.xref_stream(xref) + inline + jpeg + b"\nEI Q\n")
    try:
        images = extract_pictures(doc, [PictureRegion(0, (100.0, 100.0, 200.0, 200.0))])
    finally:
        doc.close()

    assert images[0].mime_type == "image/jpeg"
    assert pymupdf.Pixmap(images[0].bytes_).width == 400
