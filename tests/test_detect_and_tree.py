"""Format detection and the flat-list-to-folder-tree conversion.

These are the two places where a subtle mistake would be invisible in a
round-trip test but obvious to a user: a misidentified format opens with the
wrong backend, and a badly built tree shows the wrong folder contents.
"""

from __future__ import annotations

import gzip
import os
import subprocess

import pytest

from archivefree.core import detect
from archivefree.core.create import CreateOptions, create_archive, default_archive_name
from archivefree.core.entry import ArchiveEntry, normalise_path
from archivefree.core.tree import build_tree, common_root, suggested_folder_name

from .conftest import have

# -- detection -----------------------------------------------------------


@pytest.mark.parametrize("fmt,extension", [
    ("zip", ".zip"), ("tar", ".tar"), ("tar.gz", ".tar.gz"),
    ("tar.bz2", ".tar.bz2"), ("tar.xz", ".tar.xz"),
])
def test_detects_what_it_creates(fmt, extension, sample_tree, tmp_path):
    source, _ = sample_tree
    archive = str(tmp_path / f"probe{extension}")
    create_archive([source], CreateOptions(destination=archive, format=fmt,
                                           base_dir=source))
    assert detect.detect_format(archive) == fmt


def test_detects_by_content_not_extension(sample_tree, tmp_path):
    """A zip named .txt is still a zip — extensions lie, magic bytes don't."""
    source, _ = sample_tree
    archive = tmp_path / "definitely-not-an-archive.txt"
    create_archive([source], CreateOptions(destination=str(archive), format="zip",
                                           base_dir=source))
    assert detect.detect_format(str(archive)) == "zip"


def test_tar_gz_is_distinguished_from_plain_gz(tmp_path):
    """Both start with the same two magic bytes; only the name separates them."""
    plain = tmp_path / "notes.txt.gz"
    with gzip.open(plain, "wb") as fh:
        fh.write(b"just text")
    assert detect.detect_format(str(plain)) == "gz"

    tarred = tmp_path / "bundle.tar.gz"
    subprocess.run(["tar", "-czf", str(tarred), "-C", str(tmp_path), "notes.txt.gz"],
                   check=True)
    assert detect.detect_format(str(tarred)) == "tar.gz"


def test_short_form_tar_extensions(tmp_path):
    """.tgz and .txz are tar archives even though the name doesn't say "tar"."""
    payload = tmp_path / "a.txt"
    payload.write_text("x")
    tgz = tmp_path / "bundle.tgz"
    subprocess.run(["tar", "-czf", str(tgz), "-C", str(tmp_path), "a.txt"], check=True)
    assert detect.detect_format(str(tgz)) == "tar.gz"


def test_plain_file_is_not_an_archive(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("hello" * 500)
    assert detect.detect_format(str(plain)) is None


def test_empty_file_is_not_an_archive(tmp_path):
    empty = tmp_path / "empty.zip"
    empty.write_bytes(b"")
    assert detect.detect_format(str(empty)) is None


def test_every_creatable_format_is_described():
    """The create dialog reads these; a missing entry would show a blank row."""
    from archivefree.core.create import CREATABLE_HINT

    for key in detect.CREATABLE:
        assert key in detect.FORMATS, f"{key} is offered but not described"
        assert detect.FORMATS[key].extensions, f"{key} has no extension"
        assert key in CREATABLE_HINT, f"{key} has no explanation for the user"


def test_all_formats_have_unique_primary_extensions():
    seen: dict[str, str] = {}
    for key, fmt in detect.FORMATS.items():
        primary = fmt.extensions[0]
        assert primary not in seen, f"{key} and {seen[primary]} both claim {primary}"
        seen[primary] = key


# -- split volumes -------------------------------------------------------


def test_split_volume_naming_schemes(tmp_path):
    """Each part of a numbered set must resolve back to the first part."""
    for index in range(1, 4):
        (tmp_path / f"data.7z.{index:03d}").write_bytes(b"x")
    third = str(tmp_path / "data.7z.003")
    assert detect.first_volume(third) == str(tmp_path / "data.7z.001")
    assert len(detect.split_volumes(third)) == 3


def test_rar_part_naming(tmp_path):
    for index in range(1, 4):
        (tmp_path / f"movie.part{index}.rar").write_bytes(b"x")
    assert detect.first_volume(str(tmp_path / "movie.part3.rar")).endswith("part1.rar")


def test_single_file_is_not_treated_as_split(tmp_path):
    lonely = tmp_path / "solo.zip"
    lonely.write_bytes(b"PK\x03\x04")
    assert detect.split_volumes(str(lonely)) == []
    assert detect.first_volume(str(lonely)) == str(lonely)


def test_missing_volumes_detected(tmp_path):
    parts = [str(tmp_path / f"x.7z.{n:03d}") for n in (1, 2, 4, 5)]
    assert detect.missing_volumes(parts) == ["part 3"]


# -- tree building -------------------------------------------------------


def make(paths):
    return [ArchiveEntry(path=p, size=10, is_dir=p.endswith("/")) for p in
            [p.rstrip("/") for p in paths]]


def test_intermediate_folders_are_synthesised():
    """Many archives list only files, never their parent directories."""
    root = build_tree([ArchiveEntry(path="a/b/c/deep.txt", size=42)])
    assert root.find("a") is not None
    assert root.find("a/b") is not None
    assert root.find("a/b/c").is_dir
    assert root.find("a/b/c/deep.txt").size == 42


def test_folder_sizes_roll_up():
    entries = [
        ArchiveEntry(path="docs/one.txt", size=100),
        ArchiveEntry(path="docs/two.txt", size=250),
        ArchiveEntry(path="docs/sub/three.txt", size=650),
    ]
    root = build_tree(entries)
    docs = root.find("docs")
    assert docs.size == 1000
    assert docs.file_count == 3
    assert root.find("docs/sub").size == 650


def test_explicit_directory_entry_keeps_its_children():
    """A directory entry listed after its contents must not wipe them."""
    entries = [
        ArchiveEntry(path="d/inside.txt", size=5),
        ArchiveEntry(path="d", is_dir=True),
    ]
    root = build_tree(entries)
    assert root.find("d").is_dir
    assert root.find("d/inside.txt") is not None
    assert len(root.find("d").children) == 1


def test_common_root_detection():
    wrapped = make(["proj/a.txt", "proj/b/c.txt"])
    assert common_root(wrapped) == "proj"

    tarbomb = make(["a.txt", "b.txt", "c/d.txt"])
    assert common_root(tarbomb) is None

    single_file = make(["only.txt"])
    assert common_root(single_file) is None


def test_walk_files_finds_everything_beneath():
    entries = [
        ArchiveEntry(path="top/a.txt", size=1),
        ArchiveEntry(path="top/sub/b.txt", size=1),
        ArchiveEntry(path="other/c.txt", size=1),
    ]
    root = build_tree(entries)
    found = {e.path for e in root.find("top").walk_files()}
    assert found == {"top/a.txt", "top/sub/b.txt"}


def test_folders_sort_before_files():
    entries = [
        ArchiveEntry(path="zebra.txt", size=1),
        ArchiveEntry(path="apple/x.txt", size=1),
    ]
    root = build_tree(entries)
    names = [n.name for n in root.sorted_children]
    assert names == ["apple", "zebra.txt"]


# -- naming --------------------------------------------------------------


@pytest.mark.parametrize("archive,expected", [
    ("photos.tar.gz", "photos"),
    ("photos.zip", "photos"),
    ("backup.tar.bz2", "backup"),
    ("thing.7z", "thing"),
    ("no-extension", "no-extension"),
])
def test_suggested_folder_name(archive, expected):
    assert suggested_folder_name(archive) == expected


def test_default_archive_name_from_folder(tmp_path):
    folder = tmp_path / "My Photos"
    folder.mkdir()
    assert default_archive_name([str(folder)], "zip") == "My Photos.zip"
    assert default_archive_name([str(folder)], "tar.gz") == "My Photos.tar.gz"


def test_default_archive_name_from_file(tmp_path):
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF")
    assert default_archive_name([str(target)], "zip") == "report.zip"


def test_normalise_path_edge_cases():
    assert normalise_path("") == ""
    assert normalise_path("/") == ""
    assert normalise_path("a//b") == "a/b"
    assert normalise_path("a/b/") == "a/b"
    assert normalise_path("./a") == "a"


# -- create options ------------------------------------------------------


def test_tar_with_password_is_refused_clearly(sample_tree, tmp_path):
    """TAR can't carry a password; say so rather than producing an unprotected file."""
    from archivefree.core.errors import ArchiveError

    if not have("7z"):
        pytest.skip("7z not installed")
    source, _ = sample_tree
    options = CreateOptions(destination=str(tmp_path / "x.tar.gz"), format="tar.gz",
                            password="secret", base_dir=source)
    with pytest.raises(ArchiveError) as excinfo:
        create_archive([source], options)
    assert "password" in excinfo.value.message.lower()
    assert not os.path.exists(options.destination), "a plain archive was left behind"
