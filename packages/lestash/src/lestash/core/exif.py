"""When a photo or screenshot was taken, from its EXIF data."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta, timezone

_EXIF_IFD = 0x8769
_DATE_TIME_ORIGINAL = 36867
_OFFSET_TIME_ORIGINAL = 36881
_DATE_TIME = 306


def taken_at(data: bytes) -> str | None:
    """The time an image was taken, in UTC ISO 8601, or None if it doesn't say.

    Uses DateTimeOriginal, else DateTime, with OffsetTimeOriginal when given. A time
    without an offset is taken as UTC.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            exif = img.getexif()
            sub = exif.get_ifd(_EXIF_IFD)
            raw = sub.get(_DATE_TIME_ORIGINAL) or exif.get(_DATE_TIME)
            offset = sub.get(_OFFSET_TIME_ORIGINAL)
    except Exception:
        return None
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.strptime(raw.strip().rstrip("\x00"), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    tz = UTC
    if isinstance(offset, str) and len(offset) == 6 and offset[0] in "+-":
        try:
            hours, minutes = int(offset[1:3]), int(offset[4:6])
        except ValueError:
            pass
        else:
            sign = 1 if offset[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    return when.replace(tzinfo=tz).astimezone(UTC).isoformat()
