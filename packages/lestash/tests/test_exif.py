"""When an image was taken, from its EXIF data."""

import io
import time

from lestash.core.exif import taken_at
from PIL import Image


def _image(fmt: str, taken: str | None = None, offset: str | None = None, base: str | None = None):
    img = Image.new("RGB", (4, 4))
    exif = Image.Exif()
    sub = {}
    if taken:
        sub[36867] = taken
    if offset:
        sub[36881] = offset
    if sub:
        exif[0x8769] = sub  # the EXIF sub-IFD; Pillow writes it only when set whole
    if base:
        exif[306] = base
    out = io.BytesIO()
    img.save(out, format=fmt, exif=exif)
    return out.getvalue()


def test_the_time_taken_with_its_offset_is_given_in_utc():
    assert taken_at(_image("PNG", "2026:11:18 01:50:40", "+01:00")) == "2026-11-18T00:50:40+00:00"


def test_a_negative_offset():
    assert taken_at(_image("JPEG", "2026:11:18 01:50:40", "-05:30")) == "2026-11-18T07:20:40+00:00"


def test_a_time_without_an_offset_is_taken_as_local_time(monkeypatch):
    # EXIF times are the camera's wall clock; without an offset, the machine's zone
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    try:
        assert taken_at(_image("JPEG", "2026:11:18 01:50:40")) == "2026-11-18T00:50:40+00:00"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_datetime_is_used_when_there_is_no_original():
    img = Image.new("RGB", (4, 4))
    exif = Image.Exif()
    exif[306] = "2025:01:02 03:04:05"
    exif[0x8769] = {36880: "+00:00"}
    out = io.BytesIO()
    img.save(out, format="PNG", exif=exif)
    assert taken_at(out.getvalue()) == "2025-01-02T03:04:05+00:00"


def test_no_exif_gives_none():
    assert taken_at(_image("PNG")) is None


def test_datetime_takes_its_own_offset():
    img = Image.new("RGB", (4, 4))
    exif = Image.Exif()
    exif[306] = "2025:01:02 03:04:05"
    exif[0x8769] = {36880: "+02:00", 36881: "-07:00"}  # OffsetTime, OffsetTimeOriginal
    out = io.BytesIO()
    img.save(out, format="PNG", exif=exif)
    assert taken_at(out.getvalue()) == "2025-01-02T01:04:05+00:00"


def test_a_malformed_time_gives_none():
    assert taken_at(_image("PNG", "not a time")) is None


def test_bytes_that_are_not_an_image_give_none():
    assert taken_at(b"\x89PNG not really") is None
