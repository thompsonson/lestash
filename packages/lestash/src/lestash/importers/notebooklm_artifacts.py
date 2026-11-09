"""NotebookLM generated artifacts from a Google Takeout, with their files (#217).

A notebook's ``Artifacts/`` folder holds one JSON description per artifact (its title,
type and generation options) and the artifact's files:

- a slide deck: a folder with the PDF, the PPTX and one image per slide;
- an audio overview (wav), a video overview (mp4), an infographic (png), an app (html).

Names are cut short, and differently for a description and its files
("…Continual Learni.json" beside "…Continual Learnin.png"), so a file belongs to the
description whose name is its own, else the longest one either name begins with.

A multi-part Takeout spreads a notebook's files over its parts, so the parts are read
together. Each artifact becomes a child item of its notebook (``type: artifact``),
and each file goes into the file store once, as a media row.
"""

from __future__ import annotations

import json
import mimetypes
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lestash.core.blobs import BlobStore
from lestash.models.item import ItemCreate, MediaCreate

NOTEBOOKLM = "Takeout/NotebookLM/"
ARTIFACTS = "/Artifacts/"
# A description's name is "<title> metadata.json", or "<title> metadata(1).json" for a
# second artifact of that title, cut to 51 bytes with ".json": "…Data met.json"
METADATA = re.compile(r" m(?:e(?:t(?:a(?:d(?:a(?:t(?:a)?)?)?)?)?)?)?(?:\(\d*\)?)?$")
CUT_AT = 46  # bytes before ".json" in a name cut short
# Which artifact type a file can belong to: its folder (a deck) or its extension
FILE_TYPES = {
    "folder": "ARTIFACT_TYPE_SLIDES",
    ".mp4": "ARTIFACT_TYPE_EXPLAINER_VIDEO",
    ".wav": "ARTIFACT_TYPE_AUDIO_OVERVIEW",
    ".png": "ARTIFACT_TYPE_INFOGRAPHIC",
    "": "ARTIFACT_TYPE_APP",
}
# A slide image's name ends "_-_Slide_7", or, for a deck downloaded as "x", "x(1)",
# "x(2)"…, "_-_Slide", "_-_Slide(1)"…, cut as short as "_-_Slid(3)" or "_-(3)"
SLIDE_NUMBERED = re.compile(r"_-_Slide_(\d+)$")
SLIDE_COPY = re.compile(r"_-(?:_Slid?e?)?(?:\((\d+)\))?$")
# Reports are imported as notes from their markdown already
SKIPPED_TYPES = frozenset({"ARTIFACT_TYPE_TAILORED_REPORT"})
MEDIA_TYPES = {".pdf": "pdf", ".png": "image", ".wav": "audio", ".mp4": "video"}


@dataclass
class Artifact:
    notebook: str  # the notebook's folder, its item's source_id
    stem: str  # the description's title part, which its files' names begin with
    description: dict[str, Any]
    files: list[tuple[Path, str]] = field(default_factory=list)  # (zip, member)
    key: str = ""  # the description's name without ".json": unique in its notebook

    @property
    def source_id(self) -> str:
        return f"{self.notebook}{ARTIFACTS}{self.key or self.stem}"

    @property
    def kind(self) -> str:
        return str(self.description.get("type", "")).removeprefix("ARTIFACT_TYPE_").lower()


def _stem(name: str) -> str:
    """A description's title part. " metadata", " metadata(1)" and their cut forms go;
    a cut form only from a name cut short, so a title ending " me" keeps it."""
    base = name.removesuffix(".json")
    full = re.sub(r" metadata(?:\(\d+\))?$", "", base)
    if full != base or len(base.encode()) < CUT_AT:
        return full
    return METADATA.sub("", base)


def _file_type(rest: str) -> str:
    """The artifact type a file below Artifacts/ fits: see FILE_TYPES."""
    if "/" in rest:
        return FILE_TYPES["folder"]
    name = rest.rsplit("/", 1)[-1]
    suffix = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
    return FILE_TYPES.get(suffix, "")


def _head(rest: str) -> str:
    """The part of a file's path below Artifacts/ that names its artifact."""
    if "/" in rest:
        return rest.split("/", 1)[0]
    return rest.rsplit(".", 1)[0] if "." in rest else rest


def slide_number(name: str, numbered_deck: bool) -> int | None:
    """A slide image's number, 1 for the first; None if the name isn't a slide's."""
    if m := SLIDE_NUMBERED.search(name):
        return int(m.group(1))
    if not numbered_deck and (m := SLIDE_COPY.search(name)):
        return int(m.group(1)) + 1 if m.group(1) else 1
    return None


def collect(zips: list[Path]) -> tuple[list[Artifact], list[str]]:
    """The artifacts in a Takeout's parts, with their files; and the files that belong
    to no description."""
    described: dict[tuple[str, str], Artifact] = {}
    files: list[tuple[str, str, Path, str]] = []  # notebook, rest, zip, member
    for path in zips:
        with zipfile.ZipFile(path) as zf:
            for member in zf.namelist():
                if (
                    not member.startswith(NOTEBOOKLM)
                    or ARTIFACTS not in member
                    or member.endswith("/")
                ):
                    continue
                notebook, rest = member.split(ARTIFACTS, 1)
                if rest.endswith(".json") and "/" not in rest:
                    try:
                        description = json.loads(zf.read(member))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(description, dict):
                        # Keyed by name: two descriptions can share a title part
                        key = rest.removesuffix(".json")
                        described[(notebook, key)] = Artifact(
                            notebook, _stem(rest), description, key=key
                        )
                elif not rest.endswith(".md"):
                    files.append((notebook, rest, path, member))
    by_notebook: dict[str, list[Artifact]] = defaultdict(list)
    for (notebook, _), artifact in described.items():
        by_notebook[notebook].append(artifact)
    unmatched = []
    for notebook, rest, path, member in files:
        head = _head(rest)
        kind = _file_type(rest)
        named = [
            a for a in by_notebook[notebook] if head.startswith(a.stem) or a.stem.startswith(head)
        ]
        # Of the right type if any is: a type NotebookLM adds later still finds its files
        candidates = [
            a for a in named if not kind or a.description.get("type") in (kind, None)
        ] or named
        exact = [a for a in candidates if a.stem == head]
        if exact or candidates:
            owner = (exact or sorted(candidates, key=lambda a: -len(a.stem)))[0]
            owner.files.append((path, member))
        else:
            unmatched.append(member)
    return sorted(described.values(), key=lambda a: a.source_id), unmatched


def _summary(artifact: Artifact, slides: int) -> str:
    title = artifact.description.get("title") or artifact.stem
    kind = artifact.kind.replace("_", " ")
    lines = [f"# {title}", f"NotebookLM {kind}" + (f", {slides} slides" if slides else "")]
    options = artifact.description.get(_options_key(artifact.description), {})
    if isinstance(options, dict):
        generation = options.get("generationOptions")
        if isinstance(generation, dict):
            for key, value in generation.items():
                if isinstance(value, str) and value:
                    lines.append(f"**{key}:** {value}")
    return "\n\n".join(lines)


def _options_key(description: dict[str, Any]) -> str:
    """The key holding a type's options: slides, audioOverview, explainerVideo…"""
    known = {"title", "type", "sources", "status"}
    return next((k for k in description if k not in known), "")


def to_items(artifacts: list[Artifact], store: BlobStore) -> tuple[list[ItemCreate], int]:
    """One child item per artifact, its files stored and attached in slide order; and
    how many files were new to the store."""
    items = []
    new_blobs = 0
    for artifact in artifacts:
        if artifact.description.get("type") in SKIPPED_TYPES:
            continue
        names = [m.rsplit("/", 1)[-1] for _, m in artifact.files]
        numbered = any(SLIDE_NUMBERED.search(n) for n in names)
        media: list[MediaCreate] = []
        slides = 0
        for path, member in sorted(artifact.files, key=lambda f: f[1]):
            mime: str | None
            name = member.rsplit("/", 1)[-1]
            suffix = Path(name).suffix.lower()
            number = slide_number(name, numbered) if "/" in member.split(ARTIFACTS, 1)[1] else None
            if number is not None and not suffix:
                media_type, mime, position, alt = "image", "image/png", number, f"Slide {number}"
                slides += 1
            elif suffix == ".pptx":
                media_type, position, alt = "file", 0, name
                mime = mimetypes.guess_type(name)[0]
            elif not suffix:
                # An app: a web page NotebookLM made, served only as a download
                media_type, mime, position, alt = "file", "text/html", 0, f"{name}.html"
            else:
                media_type = MEDIA_TYPES.get(suffix, "file")
                mime, position, alt = mimetypes.guess_type(name)[0], 0, name
            with zipfile.ZipFile(path) as zf, zf.open(member) as stream:
                blob = store.put_stream(stream)
            new_blobs += blob.new
            media.append(
                MediaCreate(
                    media_type=media_type,
                    mime_type=mime,
                    alt_text=alt,
                    position=position,
                    source_origin="takeout",
                    sha256=blob.sha256,
                    size=blob.size,
                )
            )
        description = artifact.description
        items.append(
            ItemCreate(
                source_type="notebooklm",
                source_id=artifact.source_id,
                title=str(description.get("title") or artifact.stem),
                content=_summary(artifact, slides),
                is_own_content=True,
                metadata={
                    "source": "takeout",
                    "type": "artifact",
                    "artifact_type": artifact.kind,
                    "status": description.get("status"),
                    "notebook_sources": [
                        s.get("sourceId", {}).get("id")
                        for s in description.get("sources") or []
                        if isinstance(s, dict)
                    ],
                    "options": description.get(_options_key(description)),
                    "slide_count": slides or None,
                    "_parent_source_id": artifact.notebook,
                },
                media=media,
            )
        )
    return items, new_blobs
