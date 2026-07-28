"""Round-trip every creatable format, then cross-check with the system's own tools.

The cross-check is the point: verifying ArchiveFree against ArchiveFree would
only prove it's self-consistent. Each archive we write is also listed by unzip /
tar / 7z, and each archive *those* tools write is opened by ArchiveFree.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from archivefree.core import detect, registry
from archivefree.core.create import CreateOptions, create_archive

from .conftest import have, run

ROUND_TRIP_FORMATS = ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz", "7z", "tar.zst"]


def compare_trees(original: str, extracted: str, expected: dict[str, bytes]) -> None:
    """Every file must come back byte-for-byte, with nothing missing or extra."""
    for rel, content in expected.items():
        target = os.path.join(extracted, rel)
        assert os.path.exists(target), f"missing after extraction: {rel}"
        with open(target, "rb") as fh:
            assert fh.read() == content, f"contents differ: {rel}"

    produced = set()
    for dirpath, _, filenames in os.walk(extracted):
        for name in filenames:
            produced.add(os.path.relpath(os.path.join(dirpath, name), extracted))
    assert produced == set(expected), (
        f"extra or missing files: {produced ^ set(expected)}"
    )


@pytest.mark.parametrize("fmt", ROUND_TRIP_FORMATS)
def test_roundtrip(fmt, sample_tree, tmp_path):
    source, expected = sample_tree
    if fmt == "7z" and not have("7z"):
        pytest.skip("7z not installed")
    if fmt == "tar.zst" and not have("zstd"):
        pytest.skip("zstd not installed")

    archive = str(tmp_path / f"test{detect.FORMATS[fmt].extensions[0]}")
    created = create_archive(
        [source], CreateOptions(destination=archive, format=fmt, base_dir=source)
    )
    assert os.path.exists(created), "archive was not created"
    assert os.path.getsize(created) > 0

    # The format we wrote must be the format we detect on the way back in.
    assert detect.detect_format(created) == fmt

    out = tmp_path / f"out-{fmt.replace('.', '-')}"
    with registry.open_archive(created) as backend:
        entries = backend.list_entries()
        names = {e.path for e in entries if not e.is_dir}
        assert names >= set(expected), f"entries missing from listing: {set(expected) - names}"
        backend.extract(str(out))

    compare_trees(source, str(out), expected)


@pytest.mark.parametrize("fmt", ROUND_TRIP_FORMATS)
def test_listed_sizes_match_disk(fmt, sample_tree, tmp_path):
    """The sizes shown in the browse view must be the real uncompressed sizes."""
    source, expected = sample_tree
    if fmt == "7z" and not have("7z"):
        pytest.skip("7z not installed")
    if fmt == "tar.zst" and not have("zstd"):
        pytest.skip("zstd not installed")

    archive = str(tmp_path / f"sizes{detect.FORMATS[fmt].extensions[0]}")
    create_archive([source], CreateOptions(destination=archive, format=fmt, base_dir=source))
    with registry.open_archive(archive) as backend:
        by_path = {e.path: e.size for e in backend.list_entries() if not e.is_dir}
    for rel, content in expected.items():
        assert by_path[rel] == len(content), f"wrong size reported for {rel}"


# -- cross-checks against the system tools -------------------------------


def test_zip_readable_by_unzip(sample_tree, tmp_path):
    if not have("unzip"):
        pytest.skip("unzip not installed")
    source, expected = sample_tree
    archive = str(tmp_path / "cross.zip")
    create_archive([source], CreateOptions(destination=archive, format="zip", base_dir=source))

    check = run("unzip", "-t", archive)
    assert check.returncode == 0, check.stdout + check.stderr

    out = tmp_path / "unzipped"
    assert run("unzip", "-q", archive, "-d", str(out)).returncode == 0
    compare_trees(source, str(out), expected)


def test_tar_readable_by_tar(sample_tree, tmp_path):
    source, expected = sample_tree
    archive = str(tmp_path / "cross.tar.gz")
    create_archive([source], CreateOptions(destination=archive, format="tar.gz",
                                           base_dir=source))
    out = tmp_path / "untarred"
    out.mkdir()
    result = run("tar", "-xzf", archive, "-C", str(out))
    assert result.returncode == 0, result.stderr
    compare_trees(source, str(out), expected)


def test_7z_readable_by_7z(sample_tree, tmp_path):
    if not have("7z"):
        pytest.skip("7z not installed")
    source, _expected = sample_tree
    archive = str(tmp_path / "cross.7z")
    create_archive([source], CreateOptions(destination=archive, format="7z", base_dir=source))
    result = run("7z", "t", archive, "-p")
    assert result.returncode == 0, result.stdout


def test_opens_archives_made_by_system_tools(sample_tree, tmp_path):
    """The other direction: archives we didn't create must open correctly."""
    source, expected = sample_tree

    cases = []
    if have("zip"):
        archive = str(tmp_path / "sys.zip")
        assert subprocess.run(["zip", "-qr", archive, "."], cwd=source).returncode == 0
        cases.append(archive)
    archive = str(tmp_path / "sys.tar.bz2")
    assert subprocess.run(["tar", "-cjf", archive, "-C", source, "."]).returncode == 0
    cases.append(archive)
    if have("7z"):
        archive = str(tmp_path / "sys.7z")
        assert subprocess.run(["7z", "a", "-bso0", "-bsp0", archive, "."],
                              cwd=source).returncode == 0
        cases.append(archive)

    for path in cases:
        out = tmp_path / ("out_" + os.path.basename(path).replace(".", "_"))
        with registry.open_archive(path) as backend:
            backend.extract(str(out))
        compare_trees(source, str(out), expected)


def test_selective_extraction(sample_tree, tmp_path):
    """Extracting two files must produce exactly those two files."""
    source, _expected = sample_tree
    archive = str(tmp_path / "sel.zip")
    create_archive([source], CreateOptions(destination=archive, format="zip", base_dir=source))

    out = tmp_path / "selective"
    with registry.open_archive(archive) as backend:
        entries = backend.list_entries()
        chosen = [e for e in entries if e.path in ("readme.txt", "docs/guide.md")]
        assert len(chosen) == 2
        backend.extract(str(out), entries=chosen)

    produced = set()
    for dirpath, _, filenames in os.walk(out):
        for name in filenames:
            produced.add(os.path.relpath(os.path.join(dirpath, name), out))
    assert produced == {"readme.txt", "docs/guide.md"}


def test_extract_selected_folder_includes_children(sample_tree, tmp_path):
    source, _ = sample_tree
    archive = str(tmp_path / "folder.tar.gz")
    create_archive([source], CreateOptions(destination=archive, format="tar.gz",
                                           base_dir=source))
    out = tmp_path / "folderout"
    with registry.open_archive(archive) as backend:
        entries = backend.list_entries()
        docs = [e for e in entries if e.path == "docs" and e.is_dir]
        if not docs:  # some formats omit explicit directory entries
            from archivefree.core.entry import ArchiveEntry

            docs = [ArchiveEntry(path="docs", is_dir=True)]
        backend.extract(str(out), entries=docs)
    assert (out / "docs" / "guide.md").exists()
    assert (out / "docs" / "nested" / "deep" / "deeper" / "buried.txt").exists()
    assert not (out / "readme.txt").exists()


# -- single-file compression --------------------------------------------


@pytest.mark.parametrize("fmt,tool", [
    ("gz", "gzip"), ("xz", "xz"), ("bz2", "bzip2"), ("zst", "zstd"), ("lz4", "lz4"),
])
def test_single_stream_creation(fmt, tool, tmp_path):
    """.gz/.xz/.bz2/.zst compress one file; verify against the system tool."""
    if not have(tool):
        pytest.skip(f"{tool} not installed")

    source = tmp_path / "notes.txt"
    payload = b"a line of text\n" * 2000
    source.write_bytes(payload)

    archive = str(tmp_path / f"notes.txt{detect.FORMATS[fmt].extensions[0]}")
    create_archive([str(source)], CreateOptions(destination=archive, format=fmt))
    assert os.path.exists(archive)
    assert os.path.getsize(archive) < len(payload), "no compression happened"

    # The system tool must be able to read what we wrote.
    decoded = subprocess.run([tool, "-dc", archive], capture_output=True)
    assert decoded.returncode == 0, decoded.stderr
    assert decoded.stdout == payload

    # And we must read it back ourselves, with the inner name recovered.
    out = tmp_path / f"back-{fmt}"
    with registry.open_archive(archive) as backend:
        entries = backend.list_entries()
        assert len(entries) == 1
        assert entries[0].path == "notes.txt"
        backend.extract(str(out))
    assert (out / "notes.txt").read_bytes() == payload


def test_single_stream_refuses_multiple_files(tmp_path):
    """These formats have no filename table, so two files cannot fit."""
    from archivefree.core.errors import ArchiveError

    a = tmp_path / "one.txt"
    b = tmp_path / "two.txt"
    a.write_text("a")
    b.write_text("b")

    with pytest.raises(ArchiveError) as excinfo:
        create_archive([str(a), str(b)],
                       CreateOptions(destination=str(tmp_path / "x.gz"), format="gz"))
    assert "one file" in excinfo.value.message
    assert excinfo.value.hint and "TAR" in excinfo.value.hint


def test_single_stream_refuses_a_folder(sample_tree, tmp_path):
    from archivefree.core.errors import ArchiveError

    source, _ = sample_tree
    with pytest.raises(ArchiveError):
        create_archive([source],
                       CreateOptions(destination=str(tmp_path / "x.xz"), format="xz"))


@pytest.mark.parametrize("fmt", ["tar.lz4", "tar.lzma"])
def test_newly_offered_tar_variants_roundtrip(fmt, sample_tree, tmp_path):
    if fmt == "tar.lz4" and not have("lz4"):
        pytest.skip("lz4 not installed")
    source, expected = sample_tree
    archive = str(tmp_path / f"t{detect.FORMATS[fmt].extensions[0]}")
    create_archive([source], CreateOptions(destination=archive, format=fmt,
                                           base_dir=source))
    out = tmp_path / f"out-{fmt.replace('.', '-')}"
    with registry.open_archive(archive) as backend:
        backend.extract(str(out))
    compare_trees(source, str(out), expected)


def test_every_offered_format_can_actually_be_created(sample_tree, tmp_path):
    """The dialog must not offer something the engine will refuse."""
    source, _ = sample_tree
    single = set(detect.CREATABLE_SINGLE)
    for key in detect.CREATABLE:
        if key in single:
            continue  # covered separately; those take one file, not a tree
        if key == "7z" and not have("7z"):
            continue
        if key == "tar.zst" and not have("zstd"):
            continue
        if key == "tar.lz4" and not have("lz4"):
            continue
        archive = str(tmp_path / f"all{detect.FORMATS[key].extensions[0]}")
        create_archive([source], CreateOptions(destination=archive, format=key,
                                               base_dir=source))
        assert os.path.exists(archive), f"{key} was offered but produced nothing"
