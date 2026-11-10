"""NotebookLM generated artifacts from a Google Takeout, with their files (#217).

A notebook's ``Artifacts/`` folder holds one JSON description per artifact (its title,
type and generation options) and the artifact's files:

- a slide deck: a folder with the PDF, the PPTX and one image per slide;
- an audio overview (wav), a video overview (mp4), an infographic (png), an app (html).

Names are cut short, and differently for a description and its files
("…Continual Learni.json" beside "…Continual Learnin.png"), so a file belongs to the
description whose name is its own, else the longest one either name begins with.

A multi-part Takeout spreads a notebook's files over its parts. Each part's files go
into the file store as it is read, and an ``Index`` of the descriptions and stored
files is kept, so the parts need not be on disk together: the Takeout sync reads one
at a time. Each artifact becomes a child item of its notebook (``type: artifact``),
and each file is a media row pointing at its blob.
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


@dataclass(frozen=True)
class StoredFile:
    member: str  # its name in the zip
    sha256: str
    size: int


@dataclass
class Index:
    """The NotebookLM artifacts' descriptions and stored files of a Takeout's parts
    read so far, by zip member name. JSON-safe, so it can be kept between parts."""

    descriptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, tuple[str, int]] = field(default_factory=dict)  # member: (sha256, size)
    new_blobs: int = 0  # files new to the store while indexing; not kept

    def to_json(self) -> dict[str, Any]:
        return {"descriptions": self.descriptions, "files": self.files}

    @classmethod
    def from_json(cls, value: dict[str, Any] | None) -> Index:
        value = value or {}
        files = {m: (str(f[0]), int(f[1])) for m, f in (value.get("files") or {}).items()}
        return cls(dict(value.get("descriptions") or {}), files)


@dataclass
class Artifact:
    notebook: str  # the notebook's folder, its item's source_id
    stem: str  # the description's title part, which its files' names begin with
    description: dict[str, Any]
    files: list[StoredFile] = field(default_factory=list)
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


def _artifact_member(member: str) -> bool:
    return member.startswith(NOTEBOOKLM) and ARTIFACTS in member and not member.endswith("/")


def index_zip(path: Path, store: BlobStore, index: Index | None = None) -> Index:
    """Add one Takeout part's artifact descriptions to ``index`` (a new one if None),
    and store its artifact files. Reports' markdown is a note already, so is skipped."""
    index = index or Index()
    with zipfile.ZipFile(path) as zf:
        for member in zf.namelist():
            if not _artifact_member(member):
                continue
            rest = member.split(ARTIFACTS, 1)[1]
            if rest.endswith(".json") and "/" not in rest:
                try:
                    description = json.loads(zf.read(member))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(description, dict):
                    index.descriptions[member] = description
            elif not rest.endswith(".md"):
                with zf.open(member) as stream:
                    blob = store.put_stream(stream)
                index.new_blobs += blob.new
                index.files[member] = (blob.sha256, blob.size)
    return index


def index_zips(zips: list[Path], store: BlobStore) -> Index:
    """Every part of one Takeout, indexed."""
    index = Index()
    for path in zips:
        index_zip(path, store, index)
    return index


def collect(index: Index, final: bool = True) -> tuple[list[Artifact], list[str]]:
    """The artifacts an index holds, with their files; and the files that belong to no
    description.

    Before an export's last part (``final=False``), a file goes only to a description of
    its own name and type: one that fits better may be in a part not read yet, and a
    media row once written stays. Until then it is unmatched.
    """
    by_notebook: dict[str, list[Artifact]] = defaultdict(list)
    artifacts = []
    for member, description in index.descriptions.items():
        notebook, rest = member.split(ARTIFACTS, 1)
        # Keyed by name: two descriptions can share a title part
        key = rest.removesuffix(".json")
        artifact = Artifact(notebook, _stem(rest), description, key=key)
        by_notebook[notebook].append(artifact)
        artifacts.append(artifact)
    unmatched = []
    for member, (sha256, size) in sorted(index.files.items()):
        notebook, rest = member.split(ARTIFACTS, 1)
        head = _head(rest)
        kind = _file_type(rest)
        named = [
            a for a in by_notebook[notebook] if head.startswith(a.stem) or a.stem.startswith(head)
        ]
        # Of the right type if any is: a type NotebookLM adds later still finds its files
        typed = [a for a in named if not kind or a.description.get("type") in (kind, None)]
        candidates = typed or named
        exact = [a for a in candidates if a.stem == head]
        if not final and not any(a.stem == head for a in typed):
            unmatched.append(member)  # its own description may be in a later part
        elif exact or candidates:
            owner = (exact or sorted(candidates, key=lambda a: -len(a.stem)))[0]
            owner.files.append(StoredFile(member, sha256, size))
        else:
            unmatched.append(member)
    return sorted(artifacts, key=lambda a: a.source_id), unmatched


def _summary(artifact: Artifact) -> str:
    """From the description alone: a part read before the others has only some of
    the files, and must not change what a later part wrote."""
    title = artifact.description.get("title") or artifact.stem
    kind = artifact.kind.replace("_", " ")
    lines = [f"# {title}", f"NotebookLM {kind}"]
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


def to_items(artifacts: list[Artifact]) -> list[ItemCreate]:
    """One child item per artifact, its stored files attached in slide order."""
    items = []
    for artifact in artifacts:
        if artifact.description.get("type") in SKIPPED_TYPES:
            continue
        names = [f.member.rsplit("/", 1)[-1] for f in artifact.files]
        numbered = any(SLIDE_NUMBERED.search(n) for n in names)
        media: list[MediaCreate] = []
        for stored in sorted(artifact.files, key=lambda f: f.member):
            member = stored.member
            mime: str | None
            name = member.rsplit("/", 1)[-1]
            suffix = Path(name).suffix.lower()
            number = slide_number(name, numbered) if "/" in member.split(ARTIFACTS, 1)[1] else None
            if number is not None and not suffix:
                media_type, mime, position, alt = "image", "image/png", number, f"Slide {number}"
            elif suffix == ".pptx":
                media_type, position, alt = "file", 0, name
                mime = mimetypes.guess_type(name)[0]
            elif not suffix:
                # An app: a web page NotebookLM made, served only as a download
                media_type, mime, position, alt = "file", "text/html", 0, f"{name}.html"
            else:
                media_type = MEDIA_TYPES.get(suffix, "file")
                mime, position, alt = mimetypes.guess_type(name)[0], 0, name
            media.append(
                MediaCreate(
                    media_type=media_type,
                    mime_type=mime,
                    alt_text=alt,
                    position=position,
                    source_origin="takeout",
                    sha256=stored.sha256,
                    size=stored.size,
                )
            )
        description = artifact.description
        items.append(
            ItemCreate(
                source_type="notebooklm",
                source_id=artifact.source_id,
                title=str(description.get("title") or artifact.stem),
                content=_summary(artifact),
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
                    "_parent_source_id": artifact.notebook,
                },
                media=media,
            )
        )
    return items
