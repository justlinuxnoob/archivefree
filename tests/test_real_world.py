"""Shapes of archive that only turn up in the wild.

Every other test in this suite uses an archive this project created. That
proves self-consistency and nothing else — each case below was found by
downloading genuine files (a PyPI sdist, a GitHub source zip, a Debian package,
an Alpine minirootfs) and watching them fail. The fixtures here reproduce those
shapes locally so the suite stays offline.
"""

from __future__ import annotations

import gzip
import os
import subprocess
import tarfile

import pytest

from archivefree.core import detect, registry
from archivefree.core.errors import UnsafePath

from .conftest import have

# -- a compressed tar whose name doesn't say "tar" -----------------------


def test_gzipped_tar_named_something_else_is_still_browsable(tmp_path):
    """Found on an Alpine minirootfs saved as .iso.

    The name is the usual clue for "is there a tar inside this gzip?", but
    downloads get renamed. Trusting the name showed a 7 MB rootfs as a single
    opaque blob instead of 425 files.
    """
    payload = tmp_path / "src"
    (payload / "usr" / "bin").mkdir(parents=True)
    (payload / "usr" / "bin" / "yes").write_bytes(b"#!/bin/sh\nyes\n")
    (payload / "etc").mkdir()
    (payload / "etc" / "hostname").write_text("alpine\n")

    misnamed = tmp_path / "rootfs.iso"
    subprocess.run(["tar", "-czf", str(misnamed), "-C", str(payload), "."],
                   check=True)

    assert detect.detect_format(str(misnamed)) == "tar.gz", \
        "a gzipped tar was judged by its name instead of its contents"

    with registry.open_archive(str(misnamed)) as backend:
        names = {e.path for e in backend.list_entries() if not e.is_dir}
    assert "usr/bin/yes" in names
    assert "etc/hostname" in names


def test_a_real_single_file_gzip_is_not_mistaken_for_a_tar(tmp_path):
    """The probe must not produce false positives on ordinary .gz files."""
    plain = tmp_path / "notes.txt.gz"
    with gzip.open(plain, "wb") as fh:
        fh.write(b"just text, no tar header here\n" * 50)

    assert detect.detect_format(str(plain)) == "gz"
    with registry.open_archive(str(plain)) as backend:
        entries = backend.list_entries()
    assert len(entries) == 1
    assert entries[0].path == "notes.txt"


def test_probing_a_huge_gzip_stays_cheap(tmp_path):
    """The probe decompresses one tar header, not the whole stream."""
    import time

    big = tmp_path / "big.gz"
    with gzip.open(big, "wb", compresslevel=1) as fh:
        # 64 MB of zeros compresses tiny but takes real time to fully expand.
        for _ in range(64):
            fh.write(b"\0" * (1024 * 1024))

    started = time.monotonic()
    detect.detect_format(str(big))
    assert time.monotonic() - started < 1.0, "detection expanded the whole stream"


# -- nested containers ---------------------------------------------------


@pytest.mark.skipif(not have("7z"), reason="7z not installed")
def test_debian_package_lists_its_members(tmp_path):
    """Found on a real hello_2.10-3_amd64.deb, which listed zero files.

    A .deb is an ar archive holding control.tar.* and data.tar.*. 7-Zip
    descends into the inner tar unasked and then reports *that* archive, whose
    entries carry no path at all — so the package looked empty.
    """
    if not have("dpkg-deb"):
        pytest.skip("dpkg-deb not installed")

    root = tmp_path / "pkg"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "hello").write_text("#!/bin/sh\necho hello\n")
    (root / "DEBIAN" / "control").write_text(
        "Package: hello\nVersion: 1.0\nArchitecture: all\n"
        "Maintainer: Test <t@example.com>\nDescription: test\n"
    )
    package = tmp_path / "hello.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build",
                    str(root), str(package)], check=True, capture_output=True)

    with registry.open_archive(str(package)) as backend:
        names = {e.path for e in backend.list_entries()}
        assert names, "a valid Debian package listed nothing at all"
        assert any(n.startswith("data.tar") for n in names), names

        out = tmp_path / "unpacked"
        backend.extract(str(out))
    produced = set(os.listdir(out))
    assert any(n.startswith("data.tar") for n in produced), produced


# -- root filesystem tarballs -------------------------------------------


def test_rootfs_style_absolute_symlinks_extract(tmp_path):
    """Found on the Alpine minirootfs, which has 332 of these.

    Refusing links that point outside the destination sounds prudent and makes
    every rootfs tarball and container layer unopenable.
    """
    staging = tmp_path / "staging"
    (staging / "bin").mkdir(parents=True)
    (staging / "bin" / "busybox").write_bytes(b"binary")
    os.symlink("/bin/busybox", staging / "bin" / "sh")
    os.symlink("../bin/busybox", staging / "bin" / "ash")

    archive = tmp_path / "rootfs.tar.gz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(staging), "."],
                   check=True)

    out = tmp_path / "out"
    with registry.open_archive(str(archive)) as backend:
        backend.extract(str(out))

    assert os.path.islink(out / "bin" / "sh")
    assert os.readlink(out / "bin" / "sh") == "/bin/busybox"
    assert os.path.islink(out / "bin" / "ash")


def test_a_link_still_cannot_be_used_to_escape(tmp_path):
    """Relaxing the link check must not weaken the real protection."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("ORIGINAL")

    archive = tmp_path / "attack.tar"
    staging = tmp_path / "staging"
    staging.mkdir()
    os.symlink(str(outside), staging / "escape")
    subprocess.run(["tar", "-cf", str(archive), "-C", str(staging), "escape"],
                   check=True)
    payload = tmp_path / "payload"
    (payload / "escape").mkdir(parents=True)
    (payload / "escape" / "secret.txt").write_text("PWNED")
    subprocess.run(["tar", "-rf", str(archive), "-C", str(payload),
                    "escape/secret.txt"], check=True)

    out = tmp_path / "dest"
    with registry.open_archive(str(archive)) as backend, pytest.raises(UnsafePath):
        backend.extract(str(out))
    assert (outside / "secret.txt").read_text() == "ORIGINAL"


# -- shapes real build tools produce ------------------------------------


def test_stored_not_deflated_zip(tmp_path):
    """GitHub's source zips store the top-level directory uncompressed."""
    import zipfile

    archive = tmp_path / "stored.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("project-1.0/README.md", "# Project\n" * 50)
        zf.writestr("project-1.0/src/main.py", "print('hi')\n")

    out = tmp_path / "out"
    with registry.open_archive(str(archive)) as backend:
        backend.extract(str(out))
    assert (out / "project-1.0" / "src" / "main.py").exists()


def test_sdist_style_tarball_with_pax_headers(tmp_path):
    """setuptools writes PAX headers; they must not appear as entries."""
    source = tmp_path / "pkg-1.0"
    source.mkdir()
    (source / "PKG-INFO").write_text("Metadata-Version: 2.1\nName: pkg\n")

    archive = tmp_path / "pkg-1.0.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        tf.add(source, arcname="pkg-1.0")

    with registry.open_archive(str(archive)) as backend:
        names = {e.path for e in backend.list_entries()}
    assert "pkg-1.0/PKG-INFO" in names
    assert not any("PaxHeader" in n for n in names), \
        f"PAX metadata leaked into the listing: {names}"


# -- containers wearing a different extension ----------------------------


ZIP_ALIAS_CASES = [
    ("comic.cbz", "cbz", "Comic book archive"),
    ("book.epub", "epub", "EPUB e-book"),
    ("lib.jar", "jar", "Java archive"),
    ("app.apk", "apk", "Android package"),
    ("doc.docx", "ooxml", "Office document"),
    ("sheet.odt", "odf", "OpenDocument file"),
    ("pkg.whl", "whl", "Python wheel"),
]


@pytest.mark.parametrize("name,key,label", ZIP_ALIAS_CASES)
def test_zip_containers_are_named_correctly(name, key, label, tmp_path):
    """A .cbz and a .docx are both ZIPs; the window should say which."""
    import zipfile

    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("content/page1.txt", "hello")
        zf.writestr("meta.xml", "<meta/>")

    assert detect.detect_format(str(archive)) == key

    with registry.open_archive(str(archive)) as backend:
        info = backend.info()
        assert info.format_label == label
        names = {e.path for e in backend.list_entries() if not e.is_dir}
    assert "content/page1.txt" in names


def test_a_plain_zip_is_still_a_plain_zip(tmp_path):
    """The alias table must not swallow ordinary archives."""
    import zipfile

    archive = tmp_path / "ordinary.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", "a")

    assert detect.detect_format(str(archive)) == "zip"
    with registry.open_archive(str(archive)) as backend:
        assert backend.info().format_label == "ZIP archive"


def test_we_do_not_hijack_documents_and_ebooks():
    """Opening a .docx is useful; becoming its default handler is not."""
    from archivefree.integration import defaults

    for mime in ("application/epub+zip",
                 "application/vnd.oasis.opendocument.text",
                 "application/java-archive"):
        assert mime not in defaults.HANDLED_TYPES, (
            f"{mime} would be taken from its real application"
        )
    # Comics have no natural owner, so claiming them is a win.
    assert "application/vnd.comicbook+zip" in defaults.HANDLED_TYPES
