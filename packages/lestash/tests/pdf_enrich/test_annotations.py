"""Unit tests for the ink-annotation classifier.

Drives the classifier directly with synthetic stroke fixtures rather than
through PyMuPDF, since constructing a PDF with ink annotations is brittle and
the classifier itself is what we care about here. End-to-end coverage against
real PyMuPDF Ink output lives in test_extractor.py.
"""

import math

from lestash.core.pdf_enrich import annotations as ann_mod


class _FakePage:
    """Minimal stand-in for pymupdf.Page used by `_classify_group`."""

    def __init__(self, width: float = 612.0, height: float = 792.0):
        self.rect = type("R", (), {"width": width, "height": height})()

    def get_text(self, clip=None):
        return ""


def _ink(strokes, bbox=None, color=None, ann_id=None, created_at=None):
    points = [pt for s in strokes for pt in s]
    if bbox is None:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    return ann_mod._RawInk(
        strokes=strokes,
        bbox=bbox,
        color=color,
        annotation_id=ann_id,
        created_at=created_at,
    )


def test_underline_is_classified():
    stroke = [(100, 500), (160, 501), (220, 500), (280, 500.5)]
    group = [_ink([stroke])]
    out = ann_mod._classify_group(group, page=_FakePage(), page_num=0)
    assert out.kind == "underline"


def test_circle_is_classified():
    cx, cy, r = 300.0, 400.0, 30.0
    stroke = [
        (cx + r * math.cos(t), cy + r * math.sin(t))
        for t in [i * (2 * math.pi / 24) for i in range(24)]
    ]
    stroke.append(stroke[0])  # close the loop
    group = [_ink([stroke])]
    out = ann_mod._classify_group(group, page=_FakePage(), page_num=0)
    assert out.kind == "circle"


def test_margin_note_is_classified():
    # In the leftmost band of a 612pt-wide page (< 18% from left)
    stroke = [(20, 100), (24, 110), (28, 120), (32, 130)]
    group = [_ink([stroke])]
    out = ann_mod._classify_group(group, page=_FakePage(), page_num=0)
    assert out.kind == "margin_note"


def test_unrecognised_strokes_fall_through_to_unclassified():
    # Random scribble in the middle of the page
    stroke = [(300, 400), (305, 410), (315, 405), (310, 415), (320, 420)]
    group = [_ink([stroke])]
    out = ann_mod._classify_group(group, page=_FakePage(), page_num=0)
    assert out.kind == "ink_unclassified"


def test_classification_propagates_metadata():
    stroke = [(100, 500), (200, 500.5), (300, 500)]
    group = [
        _ink(
            [stroke],
            color="#ff0000",
            ann_id="abc-123",
            created_at="D:20260415120000Z",
        )
    ]
    out = ann_mod._classify_group(group, page=_FakePage(), page_num=2)
    assert out.color == "#ff0000"
    assert out.annotation_id == "abc-123"
    assert out.created_at == "D:20260415120000Z"
    assert out.page == 2
    assert out.stroke_geometry_hash  # non-empty hash


def test_stroke_grouping_merges_nearby_strokes():
    # Two strokes whose bbox centers are 10pt apart — should merge
    s1 = [(100, 100), (110, 100)]
    s2 = [(115, 100), (125, 100)]
    groups = ann_mod._group_strokes([_ink([s1]), _ink([s2])])
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_stroke_grouping_keeps_distant_strokes_separate():
    # Two strokes 200pt apart — distinct annotations
    s1 = [(100, 100), (110, 100)]
    s2 = [(400, 100), (410, 100)]
    groups = ann_mod._group_strokes([_ink([s1]), _ink([s2])])
    assert len(groups) == 2


def test_geometry_hash_is_stable_across_jitter():
    # Same shape, sub-2-decimal jitter → same hash
    a = [[(10.001, 20.002), (30.003, 40.004)]]
    b = [[(10.0009, 20.0021), (30.0029, 40.0041)]]
    assert ann_mod._hash_strokes(a) == ann_mod._hash_strokes(b)


def test_geometry_hash_changes_with_real_difference():
    a = [[(10.0, 20.0), (30.0, 40.0)]]
    b = [[(10.0, 20.0), (30.0, 41.0)]]
    assert ann_mod._hash_strokes(a) != ann_mod._hash_strokes(b)
