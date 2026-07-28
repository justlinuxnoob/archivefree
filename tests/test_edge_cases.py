"""Passwords, split volumes, corruption, collisions and malicious archives."""

from __future__ import annotations

import os
import subprocess
import zipfile

import pytest

from archivefree.core import detect, registry
from archivefree.core.conflict import ConflictResolver, Resolution, unique_path
from archivefree.core.create import CreateOptions, create_archive
from archivefree.core.entry import normalise_path
from archivefree.core.errors import (
    CorruptArchive,
    PasswordRequired,
    UnsafePath,
    UnsupportedFormat,
    WrongPassword,
)
from archivefree.core.jobs import Cancelled, Job, Progress

from .conftest import have, run

# -- passwords -----------------------------------------------------------


@pytest.mark.parametrize("fmt", ["zip", "7z"])
def test_password_roundtrip(fmt, sample_tree, tmp_path):
    if not have("7z"):
        pytest.skip("7z not installed")
    source, expected = sample_tree
    archive = str(tmp_path / f"secret{detect.FORMATS[fmt].extensions[0]}")
    create_archive([source], CreateOptions(destination=archive, format=fmt,
                                           password="hunter2", base_dir=source))
    assert os.path.exists(archive)

    # Wrong password must be reported as such, not as corruption.
    with registry.open_archive(archive, password="wrong") as backend, \
            pytest.raises((WrongPassword, PasswordRequired)):
        backend.extract(str(tmp_path / "nope"))

    out = tmp_path / "unlocked"
    with registry.open_archive(archive, password="hunter2") as backend:
        backend.extract(str(out))
    for rel, content in expected.items():
        with open(out / rel, "rb") as fh:
            assert fh.read() == content


def test_encrypted_archive_is_flagged_when_listing(sample_tree, tmp_path):
    """The UI needs to know an archive is encrypted before it asks for a password."""
    if not have("7z"):
        pytest.skip("7z not installed")
    source, _ = sample_tree
    archive = str(tmp_path / "flagged.zip")
    create_archive([source], CreateOptions(destination=archive, format="zip",
                                           password="pw", base_dir=source))
    with registry.open_archive(archive) as backend:
        entries = backend.list_entries()
        assert any(e.encrypted for e in entries if not e.is_dir)


def test_7z_header_encryption_requires_password_to_list(sample_tree, tmp_path):
    """With -mhe=on even the file names are secret, so listing must prompt."""
    if not have("7z"):
        pytest.skip("7z not installed")
    source, _ = sample_tree
    archive = str(tmp_path / "hidden.7z")
    create_archive([source], CreateOptions(destination=archive, format="7z",
                                           password="pw", encrypt_names=True,
                                           base_dir=source))
    with registry.open_archive(archive) as backend, \
            pytest.raises((PasswordRequired, WrongPassword)):
        backend.list_entries()
    with registry.open_archive(archive, password="pw") as backend:
        assert backend.list_entries()


# -- split volumes -------------------------------------------------------


def test_split_archive_creation_and_reassembly(sample_tree, tmp_path):
    if not have("7z"):
        pytest.skip("7z not installed")
    source, expected = sample_tree
    archive = str(tmp_path / "split.7z")
    first = create_archive([source], CreateOptions(destination=archive, format="7z",
                                                   split_bytes=64 * 1024,
                                                   base_dir=source))
    volumes = sorted(p for p in os.listdir(tmp_path) if ".7z.0" in p)
    assert len(volumes) > 1, f"expected multiple volumes, got {volumes}"
    assert first.endswith(".001")

    # Opening any part must open the whole set.
    later = str(tmp_path / volumes[1])
    assert detect.first_volume(later) == first

    out = tmp_path / "rejoined"
    with registry.open_archive(later) as backend:
        assert backend.info().volumes, "split volumes were not detected"
        backend.extract(str(out))
    for rel, content in expected.items():
        with open(out / rel, "rb") as fh:
            assert fh.read() == content


def test_missing_volume_is_reported_clearly(sample_tree, tmp_path):
    if not have("7z"):
        pytest.skip("7z not installed")
    source, _ = sample_tree
    archive = str(tmp_path / "gappy.7z")
    create_archive([source], CreateOptions(destination=archive, format="7z",
                                           split_bytes=64 * 1024, base_dir=source))
    volumes = sorted(p for p in os.listdir(tmp_path) if ".7z.0" in p)
    assert len(volumes) >= 3
    os.unlink(tmp_path / volumes[1])  # punch a hole in the middle

    parts = detect.split_volumes(str(tmp_path / volumes[0]))
    assert detect.missing_volumes(parts), "gap in volume sequence was not noticed"


# -- corruption ----------------------------------------------------------


def test_truncated_zip_reports_corruption(sample_tree, tmp_path):
    source, _ = sample_tree
    archive = tmp_path / "broken.zip"
    create_archive([source], CreateOptions(destination=str(archive), format="zip",
                                           base_dir=source))
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])  # lose the central directory

    with pytest.raises(CorruptArchive), registry.open_archive(str(archive)) as backend:
        backend.list_entries()


def test_corrupt_gzip_reports_corruption(tmp_path):
    archive = tmp_path / "broken.tar.gz"
    archive.write_bytes(b"\x1f\x8b\x08\x00" + os.urandom(500))
    with pytest.raises(CorruptArchive), registry.open_archive(str(archive)) as backend:
        backend.list_entries()


def test_non_archive_is_rejected_politely(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("just some text, definitely not an archive")
    with pytest.raises(UnsupportedFormat) as excinfo:
        registry.open_archive(str(plain))
    # The message is shown verbatim to a non-technical user.
    assert "notes.txt" in excinfo.value.message


def test_test_command_finds_damage(sample_tree, tmp_path):
    source, _ = sample_tree
    archive = tmp_path / "damaged.zip"
    create_archive([source], CreateOptions(destination=str(archive), format="zip",
                                           base_dir=source))
    data = bytearray(archive.read_bytes())
    # Corrupt the middle of the compressed data, leaving the directory intact.
    for i in range(200, 400):
        data[i] ^= 0xFF
    archive.write_bytes(bytes(data))

    with registry.open_archive(str(archive)) as backend:
        assert backend.test(), "integrity check passed on a corrupted archive"


# -- malicious archives --------------------------------------------------


def test_zip_slip_is_blocked(tmp_path):
    """An entry named ../../evil must never be written outside the destination."""
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")
        zf.writestr("safe.txt", "fine")

    out = tmp_path / "dest"
    with registry.open_archive(str(archive)) as backend:
        entries = backend.list_entries()
        # Normalisation strips the traversal before it ever reaches the disk.
        assert all(".." not in e.path for e in entries)
        backend.extract(str(out))

    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()
    assert (out / "evil.txt").exists()  # neutralised, kept inside the destination


def test_absolute_paths_are_neutralised():
    assert normalise_path("/etc/passwd") == "etc/passwd"
    assert normalise_path("../../../etc/shadow") == "etc/shadow"
    assert normalise_path("C:\\Windows\\system32") == "Windows/system32"
    assert normalise_path("a/../../b") == "b"
    assert normalise_path("./foo/./bar") == "foo/bar"


def test_symlink_escape_is_blocked(tmp_path):
    """A symlink pointing outside the destination must be refused."""
    archive = tmp_path / "link.tar"
    staging = tmp_path / "staging"
    staging.mkdir()
    os.symlink("/etc/passwd", staging / "escape")
    subprocess.run(["tar", "-cf", str(archive), "-C", str(staging), "."], check=True)

    out = tmp_path / "linkdest"
    with registry.open_archive(str(archive)) as backend, pytest.raises(UnsafePath):
        backend.extract(str(out))
    assert not os.path.lexists(out / "escape") or not os.path.exists(out / "escape")


# -- collisions ----------------------------------------------------------


def test_conflicts_never_overwrite_silently(sample_tree, tmp_path):
    source, _ = sample_tree
    archive = str(tmp_path / "conflict.zip")
    create_archive([source], CreateOptions(destination=archive, format="zip",
                                           base_dir=source))
    out = tmp_path / "dest"
    out.mkdir()
    existing = out / "readme.txt"
    existing.write_text("PRECIOUS ORIGINAL")

    asked: list[str] = []

    class Recorder(ConflictResolver):
        def ask(self, target, entry):
            asked.append(target)
            return Resolution.SKIP

    resolver = Recorder()
    with registry.open_archive(archive) as backend:
        backend.extract(str(out), on_conflict=resolver.resolve)

    assert asked, "extraction overwrote an existing file without asking"
    assert existing.read_text() == "PRECIOUS ORIGINAL"


def test_conflict_rename_keeps_both(sample_tree, tmp_path):
    source, _ = sample_tree
    archive = str(tmp_path / "rename.zip")
    create_archive([source], CreateOptions(destination=archive, format="zip",
                                           base_dir=source))
    out = tmp_path / "dest"
    out.mkdir()
    (out / "readme.txt").write_text("ORIGINAL")

    resolver = ConflictResolver(default=Resolution.RENAME)
    with registry.open_archive(archive) as backend:
        backend.extract(str(out), on_conflict=resolver.resolve)

    assert (out / "readme.txt").read_text() == "ORIGINAL"
    assert (out / "readme (2).txt").exists()


def test_unique_path_handles_compound_extensions(tmp_path):
    target = tmp_path / "backup.tar.gz"
    target.write_text("x")
    assert os.path.basename(unique_path(str(target))) == "backup (2).tar.gz"


# -- cancellation --------------------------------------------------------


def test_cancellation_stops_extraction(sample_tree, tmp_path):
    source, _ = sample_tree
    archive = str(tmp_path / "cancel.zip")
    create_archive([source], CreateOptions(destination=archive, format="zip",
                                           base_dir=source))

    progress = Progress()
    progress.cancel()  # cancelled before it even starts
    with registry.open_archive(archive) as backend, pytest.raises(Cancelled):
        backend.extract(str(tmp_path / "cancelled"), progress=progress)


def test_job_reports_errors_without_raising(tmp_path):
    """A failing job must surface the error through the callback, not crash."""
    captured: list[BaseException] = []

    def boom(progress=None):
        raise ValueError("expected")

    job = Job(boom, on_error=captured.append).start()
    job.finished.wait(timeout=5)
    assert isinstance(captured[0], ValueError)


# -- single-stream formats -----------------------------------------------


@pytest.mark.parametrize("ext,cmd", [(".gz", "gzip"), (".bz2", "bzip2"), (".xz", "xz")])
def test_plain_compressed_file_browses_as_one_entry(ext, cmd, tmp_path):
    if not have(cmd):
        pytest.skip(f"{cmd} not installed")
    source = tmp_path / "notes.txt"
    payload = b"hello from a single stream\n" * 200
    source.write_bytes(payload)
    assert run(cmd, str(source)).returncode == 0

    archive = str(source) + ext
    with registry.open_archive(archive) as backend:
        entries = backend.list_entries()
        assert len(entries) == 1
        assert entries[0].path == "notes.txt"
        out = tmp_path / "out"
        backend.extract(str(out))
    assert (out / "notes.txt").read_bytes() == payload
