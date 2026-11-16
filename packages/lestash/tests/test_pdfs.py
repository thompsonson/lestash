"""PDFs you add, and their versions (#338)."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from lestash.core.database import get_connection, get_media_dir
from lestash.core.pdfs import (
    VersionError,
    add_pdf,
    find_pdf,
    link_revision,
    older_problem,
    versions,
)
from typer.testing import CliRunner


@pytest.fixture
def no_docling():
    with patch("lestash.core.text_extract.convert_to_markdown", return_value="# text"):
        yield


def _add(conn, config, data: bytes, name: str = "paper.pdf") -> int:
    added = add_pdf(conn, config, data, name)
    conn.commit()
    return added.item_id


def test_a_pdf_becomes_an_item_with_its_file(test_db, no_docling) -> None:
    data = b"%PDF-1.4 one"
    with get_connection(test_db) as conn:
        added = add_pdf(conn, test_db, data, "Typing an Ontology.pdf")
        conn.commit()
        assert added.new
        row = conn.execute(
            "SELECT source_type, title, content, metadata FROM items WHERE id = ?",
            (added.item_id,),
        ).fetchone()
        media = conn.execute(
            "SELECT media_type, local_path, source_origin FROM item_media WHERE item_id = ?",
            (added.item_id,),
        ).fetchone()
    assert row["source_type"] == "pdf"
    assert row["title"] == "Typing an Ontology"
    assert row["content"] == "# text"
    assert hashlib.sha256(data).hexdigest() in row["metadata"]
    assert media["media_type"] == "source_pdf" and media["source_origin"] == "upload"
    assert (get_media_dir(test_db) / media["local_path"]).read_bytes() == data


def test_the_same_file_is_not_added_twice(test_db, no_docling) -> None:
    with get_connection(test_db) as conn:
        first = add_pdf(conn, test_db, b"%PDF same", "a.pdf")
        again = add_pdf(conn, test_db, b"%PDF same", "renamed.pdf")
        conn.commit()
        assert again == type(first)(first.item_id, False, first.sha256)
        assert find_pdf(conn, first.sha256) == first.item_id
        (count,) = conn.execute("SELECT COUNT(*) FROM items").fetchone()
    assert count == 1


def test_versions_form_a_line_newest_first(test_db, no_docling) -> None:
    with get_connection(test_db) as conn:
        v1, v2, v3 = (_add(conn, test_db, f"%PDF v{n}".encode()) for n in (1, 2, 3))
        assert link_revision(conn, v2, v1)
        assert link_revision(conn, v3, v2)
        assert not link_revision(conn, v3, v2)  # already recorded
        conn.commit()
        for item in (v1, v2, v3):
            assert versions(conn, item) == [v3, v2, v1]
        (kind, origin, status) = conn.execute(
            "SELECT kind, origin, status FROM item_links WHERE from_id = ? AND to_id = ?",
            (v3, v2),
        ).fetchone()
    assert (kind, origin, status) == ("revision_of", "manual", "confirmed")


def test_a_link_that_would_break_the_line_is_refused(test_db, no_docling) -> None:
    with get_connection(test_db) as conn:
        v1, v2, v3 = (_add(conn, test_db, f"%PDF v{n}".encode()) for n in (1, 2, 3))
        link_revision(conn, v2, v1)
        with pytest.raises(VersionError, match="already has a next version"):
            link_revision(conn, v3, v1)  # a branch
        with pytest.raises(VersionError, match="already the next version"):
            link_revision(conn, v2, v3)  # a second version before v2
        with pytest.raises(VersionError, match="later version"):
            link_revision(conn, v1, v2)  # a loop
        with pytest.raises(VersionError, match="itself"):
            link_revision(conn, v1, v1)
        with pytest.raises(VersionError, match="doesn't exist"):
            link_revision(conn, v3, 9999)
        assert older_problem(conn, v1) and older_problem(conn, 9999)
        assert older_problem(conn, v2) is None


def test_a_removed_revision_link_can_be_made_again(test_db, no_docling) -> None:
    from lestash.core.links import reject_link

    with get_connection(test_db) as conn:
        v1, v2 = (_add(conn, test_db, f"%PDF v{n}".encode()) for n in (1, 2))
        link_revision(conn, v2, v1)
        reject_link(conn, v2, v1, "revision_of")
        assert versions(conn, v2) == [v2]
        assert link_revision(conn, v2, v1)
        assert versions(conn, v1) == [v2, v1]


@pytest.fixture
def cli(test_db, monkeypatch, no_docling):
    from lestash.cli.main import app

    monkeypatch.setattr("lestash.core.database.Config.load", lambda: test_db)
    enriched: list[int] = []

    def enrich(item_id, config=None):
        enriched.append(item_id)
        return SimpleNamespace(status="ok", message="")

    monkeypatch.setattr("lestash.core.pdf_enrich.enrich_item", enrich)

    def run(*args: str):
        result = CliRunner().invoke(app, ["pdf", *args])
        # Rich wraps at the terminal's width: compare the words, not the lines
        return SimpleNamespace(exit_code=result.exit_code, output=" ".join(result.output.split()))

    run.enriched = enriched  # type: ignore[attr-defined]
    return run


def _file(tmp_path: Path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_cli_adds_to_a_collection_and_links_a_version(cli, test_db, tmp_path) -> None:
    with get_connection(test_db) as conn:
        cid = conn.execute("INSERT INTO collections (name) VALUES ('Learning')").lastrowid
        conn.commit()
    one = _file(tmp_path, "v1.pdf", b"%PDF v1")
    result = cli("add", one, "--collection", "Learning")
    assert result.exit_code == 0, result.output
    with get_connection(test_db) as conn:
        (v1,) = conn.execute("SELECT id FROM items").fetchone()
    two = _file(tmp_path, "v2.pdf", b"%PDF v2")
    result = cli("add", two, "--revision-of", str(v1), "-c", "Learning")
    assert result.exit_code == 0, result.output
    result = cli("add", one)
    assert "already held" in result.output
    with get_connection(test_db) as conn:
        (v2,) = conn.execute("SELECT id FROM items WHERE id != ?", (v1,)).fetchone()
        members = [
            r[0]
            for r in conn.execute(
                "SELECT item_id FROM collection_items WHERE collection_id = ? ORDER BY position",
                (cid,),
            )
        ]
        assert versions(conn, v1) == [v2, v1]
    assert members == [v1, v2]
    assert cli.enriched == [v1, v2]  # type: ignore[attr-defined]
    listed = cli("versions", str(v1))
    assert listed.exit_code == 0
    assert listed.output.index(f"{v2} ") < listed.output.index(f"{v1} ")


def test_cli_refuses_a_bad_version_before_adding(cli, test_db, tmp_path) -> None:
    result = cli("add", _file(tmp_path, "v2.pdf", b"%PDF v2"), "--revision-of", "9999")
    assert result.exit_code == 1
    assert "doesn't exist" in result.output
    with get_connection(test_db) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM items").fetchone()
    assert count == 0


def test_cli_refuses_an_unknown_collection_and_non_pdfs(cli, tmp_path) -> None:
    pdf = _file(tmp_path, "a.pdf", b"%PDF a")
    assert "No collection" in cli("add", pdf, "-c", "Nope").output
    assert "not a PDF" in cli("add", _file(tmp_path, "a.txt", b"text")).output


def test_cli_revision_command(cli, test_db, tmp_path) -> None:
    with get_connection(test_db) as conn:
        v1, v2 = (_add(conn, test_db, f"%PDF v{n}".encode()) for n in (1, 2))
    assert cli("revision", str(v2), str(v1)).exit_code == 0
    assert "already recorded" in cli("revision", str(v2), str(v1)).output
    bad = cli("revision", str(v1), str(v2))
    assert bad.exit_code == 1 and "later version" in bad.output
