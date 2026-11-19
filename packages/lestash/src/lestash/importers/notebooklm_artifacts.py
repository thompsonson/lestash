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
at a time, and writes the artifacts once the last is read. Each artifact becomes a
child item of its notebook (``type: artifact``), and each file is a media row pointing
at its blob.

Two artifacts of one title and type are "<title> metadata.json" and "<title>
metadata(1).json", with files "<title>.png" and "<title>.png(1)" (#375). Within an
export the "(1)"s pair, but which artifact gets them changes between exports, so each
keeps its name by its description, not by Takeout's order.

An export's files are dated by its request time: a file that changed between exports is
a new version, the older one superseded (#376).
"""

from __future__ import annotations

import json
import mimetypes
import re
import zipfile
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
REPORT = "ARTIFACT_TYPE_TAILORED_REPORT"
SKIPPED_TYPES = frozenset({REPORT})
# A file artifact (an upload NotebookLM keeps) named "notes.md" is "notes.md.md" (#377)
FILE_ARTIFACT = "ARTIFACT_TYPE_FILE"
MEDIA_TYPES = {".pdf": "pdf", ".png": "image", ".wav": "audio", ".mp4": "video"}
# Takeout's "(n)" on the files of the n-th other artifact of a title: "X.png(1)", a
# deck's folder "X(1)/", an app "X(1)"
COPY = re.compile(r"\((\d+)\)$")
# How far apart one request's parts' times can be: "…T100906Z-001.zip" beside
# "…T100907Z-1-001.zip" (#378)
SAME_REQUEST = timedelta(seconds=2)
# The request time in a part's name, "takeout-20261106T100907Z-1-001.zip"
REQUEST_TIME = re.compile(r"takeout-(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z")


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
    copy: int = 0  # n in "<title> metadata(n).json", the n-th other one of its title

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


def _copy(name: str) -> int:
    """n in a description's name "<title> metadata(n).json" (or a cut form), else 0."""
    removed = name.removesuffix(".json")[len(_stem(name)) :]
    m = re.search(r"\((\d+)", removed)
    return int(m.group(1)) if m else 0


def _file_copy(rest: str) -> tuple[str, int]:
    """A file's path below Artifacts/ without a "(n)" Takeout added, and n; (rest, 0)
    if none. A title may end "(2)" itself: collect() decides which it is."""
    if "/" in rest:
        folder, inner = rest.split("/", 1)
        if m := COPY.search(folder):
            return f"{folder[: m.start()]}/{inner}", int(m.group(1))
    elif m := COPY.search(rest):
        return rest[: m.start()], int(m.group(1))
    return rest, 0


def plain_name(name: str) -> str:
    """A file's name without the "(n)" after its extension: "X.png(1)" is a png."""
    return re.sub(r"(\.[A-Za-z0-9]+)\(\d+\)$", r"\1", name)


def _file_type(rest: str) -> str:
    """The artifact type a file below Artifacts/ fits: see FILE_TYPES."""
    if "/" in rest:
        return FILE_TYPES["folder"]
    name = rest.rsplit("/", 1)[-1]
    suffix = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
    if suffix == ".md":
        return FILE_ARTIFACT  # a report's markdown is a note, and not matched
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


def _is_description(rest: str) -> bool:
    return rest.endswith(".json") and "/" not in rest


def _a_reports_markdown(member: str, descriptions: Mapping[str, dict[str, Any]]) -> bool:
    """Whether a markdown file below Artifacts/ is a report's, a note already: the
    description it best fits is a report's. One no description yet fits may be a file
    artifact's, so it is kept."""
    notebook, rest = member.split(ARTIFACTS, 1)
    head = _head(rest)
    fits = []
    for name, description in descriptions.items():
        nb, desc = name.split(ARTIFACTS, 1)
        stem = _stem(desc)
        if nb == notebook and (head.startswith(stem) or stem.startswith(head)):
            fits.append((stem != head, -len(stem), description.get("type")))
    return bool(fits) and min(fits)[2] == REPORT


def index_zip(
    path: Path,
    store: BlobStore,
    index: Index | None = None,
    known: Mapping[str, dict[str, Any]] | None = None,
) -> Index:
    """Add one Takeout part's artifact descriptions to ``index`` (a new one if None),
    and store its artifact files. A report's markdown is a note already, so is skipped;
    a file artifact's is kept (#377). ``known``: descriptions from the export's other
    parts, to tell the two apart."""
    index = index or Index()
    with zipfile.ZipFile(path) as zf:
        members = [m for m in zf.namelist() if _artifact_member(m)]
        for member in members:
            if not _is_description(member.split(ARTIFACTS, 1)[1]):
                continue
            try:
                description = json.loads(zf.read(member))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(description, dict):
                index.descriptions[member] = description
        descriptions = {**(known or {}), **index.descriptions}
        for member in members:
            rest = member.split(ARTIFACTS, 1)[1]
            if _is_description(rest):
                continue
            if rest.endswith(".md") and _a_reports_markdown(member, descriptions):
                continue
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


def collect(
    index: Index, final: bool = True, stored: Mapping[str, StoredItem] | None = None
) -> tuple[list[Artifact], list[str]]:
    """The artifacts an index holds, with their files; and the files that belong to no
    description. A file artifact's markdown goes only to a file artifact: else it is a
    report's, a note.

    Before an export's last part (``final=False``) there are none, and every file is
    unmatched: a part not read yet may hold a better description for a file, or another
    artifact of a title, which changes the names (see _name). A media
    row or an item written then could stand for the wrong artifact, so nothing is
    written until the export is read (#375).

    ``stored``: the identities of the artifact items stored already, by source_id (see
    stored_identities), so each keeps standing for the artifact it was made from.
    """
    if not final:
        return [], sorted(index.files)
    by_notebook: dict[str, list[Artifact]] = defaultdict(list)
    artifacts = []
    for member, description in index.descriptions.items():
        notebook, rest = member.split(ARTIFACTS, 1)
        # Keyed by name: two descriptions can share a title part
        key = rest.removesuffix(".json")
        artifact = Artifact(notebook, _stem(rest), description, key=key, copy=_copy(rest))
        by_notebook[notebook].append(artifact)
        artifacts.append(artifact)
    unmatched = []
    for member, (sha256, size) in sorted(index.files.items()):
        notebook, rest = member.split(ARTIFACTS, 1)
        owner = _owner(by_notebook[notebook], rest)
        if owner is not None:
            owner.files.append(StoredFile(member, sha256, size))
        elif not rest.endswith(".md"):
            unmatched.append(member)
    held = _stored_by_title(stored or {})
    for group in _by_title(artifacts):
        title = (group[0].notebook, group[0].stem)
        if len(group) > 1 or held.get(title):
            _name(group, held.get(title, {}))
    names = [a.source_id for a in artifacts]
    if len(set(names)) < len(names):  # _name gives each a name no other has
        raise RuntimeError(f"two NotebookLM artifacts named alike: {sorted(names)}")
    return sorted(artifacts, key=lambda a: a.source_id), unmatched


def _of_type(artifact: Artifact, kind: str) -> bool:
    return not kind or artifact.description.get("type") in (kind, None)


def _owner(artifacts: list[Artifact], rest: str) -> Artifact | None:
    """The artifact a file below Artifacts/ belongs to, or None.

    Takeout numbers a title's descriptions across all its artifacts, whatever their
    types ("X metadata.json" a deck, "X metadata(1).json" a video), but its files
    only among files of one name ("X.mp4", "X.png", "X.png(1)"). So a file's "(n)"
    is its rank among the descriptions of its type, in Takeout's order: the n-th
    infographic of the title gets "X.png(n)", whatever number its description has.
    A rank with no description is no artifact's file.
    """
    plain, copy = _file_copy(rest)
    head = _head(plain)
    kind = _file_type(plain)
    # "X.png(1)" is always Takeout's "(1)"; "X(1)/" or "X(1)" may be a title: it is
    # one unless the title has that many artifacts of the file's type
    if (
        copy
        and ("/" in rest or plain_name(rest) == rest)
        and sum(a.stem == head and _of_type(a, kind) for a in artifacts) <= copy
    ):
        plain, copy, head = rest, 0, _head(rest)
        kind = _file_type(plain)
    named = [a for a in artifacts if head.startswith(a.stem) or a.stem.startswith(head)]
    # Of the right type if any is: a type NotebookLM adds later still finds its files
    typed = [a for a in named if _of_type(a, kind)]
    if kind == FILE_ARTIFACT:
        named = typed  # a report's markdown: no artifact's file
    candidates = typed or named
    if not candidates:
        return None
    # Its own title, else the longest a name cut short begins with
    stem = (
        head
        if any(a.stem == head for a in candidates)
        else max((a.stem for a in candidates), key=len)
    )
    ranked = sorted((a for a in candidates if a.stem == stem), key=lambda a: a.copy)
    return ranked[copy] if copy < len(ranked) else None


def _by_title(artifacts: list[Artifact]) -> list[list[Artifact]]:
    """The artifacts grouped by notebook and title part, of every type: Takeout's
    names ("<title> metadata.json", "<title> metadata(1).json") are shared by all of
    a title's artifacts in a notebook, whatever their types."""
    groups: dict[tuple[str, str], list[Artifact]] = defaultdict(list)
    for a in artifacts:
        groups[(a.notebook, a.stem)].append(a)
    return list(groups.values())


@dataclass(frozen=True)
class StoredItem:
    """What tells a stored artifact item from another of its title."""

    identity: str  # its sources and options: see _identity
    kind: str  # its artifact type, "infographic"; "" if not recorded


def _stored_by_title(
    stored: Mapping[str, StoredItem],
) -> dict[tuple[str, str], dict[str, StoredItem]]:
    """The stored items by name (key), by notebook and title part."""
    out: dict[tuple[str, str], dict[str, StoredItem]] = defaultdict(dict)
    for source_id, item in stored.items():
        if ARTIFACTS not in source_id:
            continue
        notebook, key = source_id.split(ARTIFACTS, 1)
        out[(notebook, _stem(f"{key}.json"))][key] = item
    return out


def _by_description(artifact: Artifact) -> tuple[int, str]:
    sources = artifact.description.get("sources")
    count = len(sources) if isinstance(sources, list) else 0
    return count, json.dumps(artifact.description, sort_keys=True, default=str)


def _name(group: list[Artifact], held: Mapping[str, StoredItem]) -> None:
    """Give a title's artifacts in a notebook, of every type, names no two share, so
    each keeps its item whichever Takeout called "(1)" this time, and however many
    there are now (#375).

    One whose type, sources and options are those of a stored item of its title takes
    that item's name, whatever names this export has. The others of each type take,
    in the order of their descriptions (then their files), the names Takeout gave that
    type's artifacts, no artifact took and no stored item of another type has; failing
    that, a new name no artifact of the export and no stored item has. A stored item
    no artifact matched is one whose artifact changed: its name stays free for its
    type, so it is updated.
    ``held``: the stored items of the title, by name.
    """
    named: dict[int, str] = {}
    by_type: dict[str, list[int]] = defaultdict(list)
    for i, a in enumerate(group):
        by_type[a.kind].append(i)
    for indexes in by_type.values():
        identities = {i: _artifact_identity(group[i]) for i in indexes}
        for i, identity in identities.items():
            if list(identities.values()).count(identity) > 1:
                continue  # the same description twice: no telling which item is which
            matches = [
                k
                for k, item in held.items()
                if item.kind in (group[i].kind, "") and item.identity == identity
            ]
            if len(matches) == 1 and matches[0] not in named.values():
                named[i] = matches[0]
    taken = set(named.values())
    takeout_names = {a.key for a in group}

    def files(a: Artifact) -> list[str]:
        return [f.sha256 for f in sorted(a.files, key=lambda f: plain_name(f.member))]

    fresh = (f"{group[0].stem} metadata({n})" for n in range(1, 100_000))
    for kind in sorted(by_type):
        indexes = by_type[kind]
        own = [group[i].key for i in sorted(indexes, key=lambda i: group[i].copy)]
        unnamed = sorted(
            (i for i in indexes if i not in named),
            key=lambda i: (_by_description(group[i]), files(group[i])),
        )
        for i in unnamed:
            # Not a stored item of another type: that would make it stand for this one
            free = [
                k for k in own if k not in taken and (k not in held or held[k].kind in (kind, ""))
            ]
            key = (
                free[0]
                if free
                else next(
                    k for k in fresh if k not in taken and k not in takeout_names and k not in held
                )
            )
            named[i] = key
            taken.add(key)
    for i, key in named.items():
        group[i].key = key


def _summary(artifact: Artifact) -> str:
    """From the description alone."""
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


def _media(artifact: Artifact) -> list[MediaCreate]:
    """An artifact's stored files as media rows, in slide order."""
    names = [f.member.rsplit("/", 1)[-1] for f in artifact.files]
    numbered = any(SLIDE_NUMBERED.search(n) for n in names)
    media: list[MediaCreate] = []
    for stored in sorted(artifact.files, key=lambda f: f.member):
        member = stored.member
        mime: str | None
        name = plain_name(member.rsplit("/", 1)[-1])
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
        elif suffix == ".md":
            # A file artifact's markdown: named as it was uploaded, not "x.md.md"
            options = artifact.description.get(_options_key(artifact.description))
            uploaded = options.get("fileName") if isinstance(options, dict) else None
            media_type, mime, position = "file", "text/markdown", 0
            alt = uploaded if isinstance(uploaded, str) and uploaded else name
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
    return media


def export_time(name: str) -> str | None:
    """When a Takeout export was requested, from a part's name or an export's key
    ("takeout-20261106T100907Z…"), as the database writes times; None if not named so."""
    if not (m := REQUEST_TIME.search(name)):
        return None
    y, mo, d, h, mi, sec = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:{sec}"


def request_groups(keys: Iterable[str], kept: Collection[str] = ()) -> dict[str, str]:
    """Each Takeout export key's group key. Keys whose request times are at most
    SAME_REQUEST apart are one request (#378). The group's key is the latest of those
    in ``kept`` (an index is stored under it), else its latest. A key with no request
    time is its own group."""
    timed = sorted((t, k) for k in set(keys) if (t := export_time(k)))
    groups: list[list[str]] = []
    last: datetime | None = None
    for when, key in timed:
        at = datetime.fromisoformat(when)
        if groups and last is not None and at - last <= SAME_REQUEST:
            groups[-1].append(key)
        else:
            groups.append([key])
        last = at
    chosen = {}
    for group in groups:
        stored = [k for k in group if k in kept]
        for key in group:
            chosen[key] = (stored or group)[-1]
    return {k: chosen.get(k, k) for k in keys}


def merge_split_exports(
    checkpoints: Mapping[str, dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], list[str]]]:
    """Exports a sync before #378 kept as two indexes, one request's parts named a
    second apart: by the key to keep, the merged index and the keys it replaces. The
    merged index is not final, so the sync completes it again."""
    exports = [k for k, v in checkpoints.items() if k.startswith("notebooklm:") and v]
    members: dict[str, list[str]] = defaultdict(list)
    for key, group in request_groups(exports, exports).items():
        members[group].append(key)
    merged = {}
    for group, keys in members.items():
        if len(keys) < 2:
            continue
        index = Index()
        for key in sorted(keys, key=lambda k: export_time(k) or ""):  # newer wins
            part = Index.from_json(checkpoints[key])
            index.descriptions.update(part.descriptions)
            index.files.update(part.files)
        merged[group] = ({**index.to_json(), "final": False}, [k for k in keys if k != group])
    return merged


@dataclass
class Export:
    """One Takeout export's artifacts, and when it was requested."""

    seen_at: str
    artifacts: list[Artifact]


def _versions(
    source_id: str, current: list[MediaCreate], seen_at: str, others: Sequence[Export]
) -> list[MediaCreate]:
    """An artifact's media with each file's versions over the exports (#376).

    At each place (type and position) a file is one version from the export it was
    first seen in, replaced by the next export's other file there; the newest export's
    is current, whichever export is read now. A place holding two files in one export
    is left as it is: there is no telling which file followed which.
    """
    sightings: dict[tuple[str, int], list[tuple[str, MediaCreate]]] = defaultdict(list)
    ambiguous: set[tuple[str, int]] = set()
    exports = [(seen_at, current)] + [
        (e.seen_at, _media(a)) for e in others for a in e.artifacts if a.source_id == source_id
    ]
    for when, media in exports:
        places = [(m.media_type, m.position) for m in media]
        ambiguous.update(p for p in places if places.count(p) > 1)
        for m in media:
            sightings[(m.media_type, m.position)].append((when, m))
    out = []
    for m in current:
        place = (m.media_type, m.position)
        if place in ambiguous:
            out.append(m)
            continue
        if place not in sightings:
            continue
        seen = sightings.pop(place)
        seen.sort(key=lambda s: s[0])
        runs: list[tuple[str, MediaCreate]] = []
        for when, version in seen:
            if not runs or runs[-1][1].sha256 != version.sha256:
                runs.append((when, version))
        for i, (when, version) in enumerate(runs):
            replaced = runs[i + 1][0] if i + 1 < len(runs) else None
            out.append(version.model_copy(update={"seen_at": when, "superseded_at": replaced}))
    return out


def to_items(
    artifacts: list[Artifact], seen_at: str | None = None, others: Sequence[Export] = ()
) -> list[ItemCreate]:
    """One child item per artifact, its stored files attached in slide order.

    With ``seen_at``, the export's request time, each file is a version seen then, and
    ``others`` (the other exports read) give its earlier and later versions.
    """
    items = []
    for artifact in artifacts:
        if artifact.description.get("type") in SKIPPED_TYPES:
            continue
        media = _media(artifact)
        if seen_at is not None:
            media = _versions(artifact.source_id, media, seen_at, others)
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
                    "notebook_sources": _source_ids(description),
                    "options": description.get(_options_key(description)),
                    "_parent_source_id": artifact.notebook,
                },
                media=media,
            )
        )
    return items


def _source_ids(description: dict[str, Any]) -> list[Any]:
    return [
        s.get("sourceId", {}).get("id")
        for s in description.get("sources") or []
        if isinstance(s, dict)
    ]


def _identity(sources: Any, options: Any) -> str:
    """What tells an artifact from another of its title: its sources and options."""
    return json.dumps([sources, options], sort_keys=True, default=str)


def _artifact_identity(artifact: Artifact) -> str:
    description = artifact.description
    return _identity(_source_ids(description), description.get(_options_key(description)))


def stored_identities(conn: Any) -> dict[str, StoredItem]:
    """Each NotebookLM artifact item stored, by source_id, with what tells it from
    another of its title: collect() names same-titled artifacts after the items they
    made."""
    rows = conn.execute(
        "SELECT source_id, metadata FROM items WHERE source_type = 'notebooklm' "
        "AND json_valid(metadata) AND json_extract(metadata, '$.type') = 'artifact'"
    ).fetchall()
    out = {}
    for source_id, text in rows:
        metadata = json.loads(text)
        out[source_id] = StoredItem(
            _identity(metadata.get("notebook_sources"), metadata.get("options")),
            str(metadata.get("artifact_type") or ""),
        )
    return out


def stored_exports(
    checkpoints: Mapping[str, dict[str, Any]], stored: Mapping[str, StoredItem] | None = None
) -> list[Export]:
    """The exports whose indexes the Takeout sync kept (``notebooklm:takeout-<time>``),
    oldest first, each collected as it was last read; ``stored`` as for collect()."""
    checkpoints = dict(checkpoints)
    for key, (value, replaced) in merge_split_exports(checkpoints).items():
        # Final if each half was: then nothing of the request is unread
        final = all(checkpoints[k].get("final", True) for k in [key, *replaced])
        checkpoints[key] = {**value, "final": final}
        for old in replaced:
            del checkpoints[old]
    exports = []
    for key, value in checkpoints.items():
        when = export_time(key) if key.startswith("notebooklm:") else None
        if when is None or not value:
            continue
        final = bool(value.get("final", True))
        artifacts, _ = collect(Index.from_json(value), final, stored)
        exports.append(Export(when, artifacts))
    return sorted(exports, key=lambda e: e.seen_at)


def refile_stored_exports(conn: Any, exports: Iterable[Export]) -> tuple[int, int]:
    """Put each stored artifact's files where the exports say (#375, #376): versions
    dated by export, so an older file is superseded; a file on the wrong artifact or
    with the wrong type removed. Only items already stored; their text is rebuilt.
    Collect ``exports`` with stored_identities, so each same-titled item keeps its
    artifact. Returns (items changed, rows removed). Does not commit."""
    from lestash.core.database import add_item_media
    from lestash.core.extract import refresh_item

    exports = list(exports)
    wanted: dict[str, dict[tuple[Any, ...], MediaCreate]] = defaultdict(dict)
    places: dict[str, set[tuple[str, int, str | None]]] = defaultdict(set)
    for i, export in enumerate(exports):
        others = exports[:i] + exports[i + 1 :]
        for item in to_items(export.artifacts, export.seen_at, others):
            for m in item.media or []:
                wanted[str(item.source_id)][(m.media_type, m.position, m.sha256, m.seen_at)] = m
        for artifact in export.artifacts:
            for m in _media(artifact):
                places[artifact.source_id].add((m.media_type, m.position, m.sha256))
    known = {sha for item_places in places.values() for _, _, sha in item_places}
    changed = removed = 0
    for source_id, by_key in sorted(wanted.items()):
        # Oldest first at each place, so versions are numbered as they came
        media = sorted(by_key.values(), key=lambda m: (m.media_type, m.position, m.seen_at or ""))
        row = conn.execute(
            "SELECT id FROM items WHERE source_type = 'notebooklm' AND source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            continue
        item_id = row[0]
        before = conn.total_changes
        # A file the exports put elsewhere, or as another type ("X.png(1)" as a file).
        # One no export has (an export read before indexing) is left alone.
        wrong = [
            r[0]
            for r in conn.execute(
                "SELECT id, media_type, position, sha256 FROM item_media "
                "WHERE item_id = ? AND source_origin = 'takeout' AND sha256 IS NOT NULL",
                (item_id,),
            )
            if r[3] in known and (r[1], r[2], r[3]) not in places[source_id]
        ]
        for media_id in wrong:
            conn.execute("DELETE FROM item_media WHERE id = ?", (media_id,))
        removed += len(wrong)
        for m in media:
            add_item_media(
                conn,
                item_id,
                media_type=m.media_type,
                mime_type=m.mime_type,
                alt_text=m.alt_text,
                position=m.position,
                source_origin=m.source_origin,
                sha256=m.sha256,
                size=m.size,
                seen_at=m.seen_at,
                superseded_at=m.superseded_at,
                _commit=False,
            )
        if conn.total_changes != before:
            changed += 1
            refresh_item(conn, item_id)
    return changed, removed
