"""When a photo or screenshot was taken, from its EXIF data."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta, timezone

_EXIF_IFD = 0x8769
_DATE_TIME_ORIGINAL = 36867
_OFFSET_TIME = 36880
_OFFSET_TIME_ORIGINAL = 36881
_DATE_TIME = 306


def taken_at(data: bytes) -> str | None:
    """The time an image was taken, in UTC ISO 8601, or None if it doesn't say.

    Uses DateTimeOriginal with OffsetTimeOriginal, else DateTime with OffsetTime. EXIF
    times are the camera's wall clock, so a time without an offset is taken as this
    machine's local time.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            exif = img.getexif()
            sub = exif.get_ifd(_EXIF_IFD)
            # Each time has its own offset tag
            if sub.get(_DATE_TIME_ORIGINAL):
                raw, offset = sub.get(_DATE_TIME_ORIGINAL), sub.get(_OFFSET_TIME_ORIGINAL)
            else:
                raw, offset = exif.get(_DATE_TIME), sub.get(_OFFSET_TIME)
    except Exception:
        return None
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.strptime(raw.strip().rstrip("\x00"), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    tz = None
    if isinstance(offset, str) and len(offset) == 6 and offset[0] in "+-":
        try:
            hours, minutes = int(offset[1:3]), int(offset[4:6])
        except ValueError:
            pass
        else:
            sign = 1 if offset[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    # astimezone() reads a naive time as local time
    local = when.replace(tzinfo=tz) if tz else when.astimezone()
    return local.astimezone(UTC).isoformat()
