"""Strip Docling artifacts from extracted markdown.

Docling emits `·` (U+00B7) and `◦` (U+25E6) at the **end** of lines as a
list-parsing artifact, e.g. `"...and preprints. ·\n"`. We strip them only when
they are the last non-whitespace character of a line, so in-content uses like
`m·s⁻¹` survive intact.
"""

import re

_TRAILING_BULLET_ARTIFACT = re.compile(r"[ \t]*[·◦][ \t]*$", re.MULTILINE)


def strip_artifacts(markdown: str) -> str:
    return _TRAILING_BULLET_ARTIFACT.sub("", markdown)
