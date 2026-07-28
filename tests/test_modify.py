"""Adding files to, and deleting files from, an existing archive.

These rewrite a file the user already has, so the safety property matters as
much as the feature: a failure or a cancellation must leave the original
untouched, never half-written.
"""

from __future__ import annotations

import os

import pytest

from archivefree.core import registry
from archivefree.core.create import CreateOptions, create_archive
from archivefree.core.jobs import Cancelled, Progress

from .conftest import have

MODIFIABLE = ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz"]


def build(tmp_path, fmt, name="archive"):
    """A small archive with known contents, in the given format."""
    from archivefree.core import detect

    source = tmp_path / f"src-{name}"
    (source / "docs").mkdir(parents=True)
    (source / "readme.txt").write_text("original readme\n")
    (source / "docs" / "guide.md").write_text("# Guide\n")
    (source / "docs" / "notes.txt").write_text("notes\n")

    archive = str(tmp_path / f"{name}{detect.FORMATS[fmt].extensions[0]}")
    create_archive([str(source)], CreateOptions(destination=archive, format=fmt,
                                                base_dir=str(source)))
    return archive


def names_in(archive):
    with registry.open_archive(archive) as backend:
        return {e.path for e in backend.list_entries() if not e.is_dir}


# -- adding --------------------------------------------------------------


@pytest.mark.parametrize("fmt", MODIFIABLE)
def test_add_a_file(fmt, tmp_path):
    archive = build(tmp_path, fmt)
    extra = tmp_path / "extra.txt"
    extra.write_text("added later\n")

    with registry.open_archive(archive) as backend:
        assert backend.supports_modification
        added = backend.add([str(extra)])
        assert added == 1

    names = names_in(archive)
    assert "extra.txt" in names
    assert "readme.txt" in names, "adding a file lost the existing contents"

    # And the new file reads back correctly.
    out = tmp_path / f"out-{fmt.replace('.', '-')}"
    with registry.open_archive(archive) as backend:
        backend.extract(str(out))
    assert (out / "extra.txt").read_text() == "added later\n"
    assert (out / "docs" / "guide.md").read_text() == "# Guide\n"


@pytest.mark.parametrize("fmt", MODIFIABLE)
def test_add_a_folder_recursively(fmt, tmp_path):
    archive = build(tmp_path, fmt, name="folder")
    extra = tmp_path / "newdir"
    (extra / "deep").mkdir(parents=True)
    (extra / "one.txt").write_text("1")
    (extra / "deep" / "two.txt").write_text("2")

    with registry.open_archive(archive) as backend:
        backend.add([str(extra)])

    names = names_in(archive)
    assert "newdir/one.txt" in names
    assert "newdir/deep/two.txt" in names


@pytest.mark.parametrize("fmt", MODIFIABLE)
def test_adding_replaces_an_entry_of_the_same_name(fmt, tmp_path):
    """Two entries with one name would make the archive ambiguous."""
    archive = build(tmp_path, fmt, name="replace")
    newer = tmp_path / "readme.txt"
    newer.write_text("REPLACED\n")

    with registry.open_archive(archive) as backend:
        backend.add([str(newer)])

    out = tmp_path / f"replaced-{fmt.replace('.', '-')}"
    with registry.open_archive(archive) as backend:
        entries = [e for e in backend.list_entries() if e.path == "readme.txt"]
        assert len(entries) == 1, "the archive now has two entries with one name"
        backend.extract(str(out))
    assert (out / "readme.txt").read_text() == "REPLACED\n"


def test_add_into_a_subfolder(tmp_path):
    archive = build(tmp_path, "zip", name="into")
    extra = tmp_path / "note.txt"
    extra.write_text("filed away\n")

    with registry.open_archive(archive) as backend:
        backend.add([str(extra)], into="docs")

    assert "docs/note.txt" in names_in(archive)


# -- deleting ------------------------------------------------------------


@pytest.mark.parametrize("fmt", MODIFIABLE)
def test_delete_a_file(fmt, tmp_path):
    archive = build(tmp_path, fmt, name="del")
    with registry.open_archive(archive) as backend:
        target = [e for e in backend.list_entries() if e.path == "readme.txt"]
        assert backend.delete(target) == 1

    names = names_in(archive)
    assert "readme.txt" not in names
    assert "docs/guide.md" in names, "deleting one entry removed others"


@pytest.mark.parametrize("fmt", MODIFIABLE)
def test_deleting_a_folder_removes_its_contents(fmt, tmp_path):
    archive = build(tmp_path, fmt, name="delfolder")
    with registry.open_archive(archive) as backend:
        from archivefree.core.entry import ArchiveEntry

        folder = [e for e in backend.list_entries() if e.path == "docs" and e.is_dir]
        if not folder:
            folder = [ArchiveEntry(path="docs", is_dir=True)]
        backend.delete(folder)

    names = names_in(archive)
    assert not any(n.startswith("docs/") for n in names), names
    assert "readme.txt" in names


@pytest.mark.parametrize("fmt", MODIFIABLE)
def test_the_archive_stays_valid_after_modification(fmt, tmp_path):
    """A rewritten archive must still pass its own integrity check."""
    archive = build(tmp_path, fmt, name="valid")
    extra = tmp_path / "x.txt"
    extra.write_text("x")

    with registry.open_archive(archive) as backend:
        backend.add([str(extra)])
    with registry.open_archive(archive) as backend:
        target = [e for e in backend.list_entries() if e.path == "docs/notes.txt"]
        backend.delete(target)
    with registry.open_archive(archive) as backend:
        assert backend.test() == [], "the archive is damaged after being modified"


# -- safety --------------------------------------------------------------


@pytest.mark.parametrize("fmt", MODIFIABLE)
def test_a_failed_modification_leaves_the_original_intact(fmt, tmp_path):
    """The whole point of writing to a temporary file first."""
    archive = build(tmp_path, fmt, name="safe")
    before = open(archive, "rb").read()

    progress = Progress()
    progress.cancel()  # cancelled before a byte is written

    with registry.open_archive(archive) as backend, pytest.raises(Cancelled):
        backend.add([str(tmp_path / "src-safe" / "readme.txt")],
                    progress=progress)

    assert open(archive, "rb").read() == before, \
        "a cancelled modification damaged the original archive"


@pytest.mark.parametrize("fmt", MODIFIABLE)
def test_no_temporary_files_are_left_behind(fmt, tmp_path):
    archive = build(tmp_path, fmt, name="tidy")
    extra = tmp_path / "y.txt"
    extra.write_text("y")
    with registry.open_archive(archive) as backend:
        backend.add([str(extra)])

    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".archivefree-")]
    assert not leftovers, f"temporary files left in place: {leftovers}"


def test_permissions_are_preserved(tmp_path):
    archive = build(tmp_path, "zip", name="perms")
    os.chmod(archive, 0o640)
    extra = tmp_path / "z.txt"
    extra.write_text("z")

    with registry.open_archive(archive) as backend:
        backend.add([str(extra)])

    assert os.stat(archive).st_mode & 0o777 == 0o640, \
        "rewriting the archive changed its permissions"


# -- formats that cannot be modified -------------------------------------


def test_read_only_formats_say_so(tmp_path):
    """A .gz holds one nameless stream; there is nothing to add to."""
    import gzip

    plain = tmp_path / "notes.txt.gz"
    with gzip.open(plain, "wb") as fh:
        fh.write(b"content\n")

    with registry.open_archive(str(plain)) as backend:
        assert not backend.supports_modification


@pytest.mark.skipif(not have("7z"), reason="7z not installed")
def test_sevenzip_archives_can_be_modified(tmp_path):
    archive = build(tmp_path, "7z", name="seven")
    extra = tmp_path / "added.txt"
    extra.write_text("via 7-Zip\n")

    with registry.open_archive(archive) as backend:
        assert backend.supports_modification
        backend.add([str(extra)])
    assert "added.txt" in names_in(archive)

    with registry.open_archive(archive) as backend:
        target = [e for e in backend.list_entries() if e.path == "added.txt"]
        backend.delete(target)
    assert "added.txt" not in names_in(archive)
