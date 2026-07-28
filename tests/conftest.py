"""Shared fixtures: a realistic sample tree used by every round-trip test."""

from __future__ import annotations

import os
import subprocess

import pytest


def make_sample_tree(root: str) -> dict[str, bytes]:
    """Create a nested folder tree and return {relative path: contents}.

    Deliberately awkward: nested folders, an empty file, a large-ish
    incompressible file, unicode and space-containing names, and a deep path.
    """
    files = {
        "readme.txt": b"ArchiveFree round-trip test\n" * 40,
        "empty.dat": b"",
        "docs/guide.md": b"# Guide\n\nSome text.\n" * 100,
        "docs/images/logo.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        "docs/nested/deep/deeper/buried.txt": b"buried treasure\n" * 10,
        "data/random.bin": os.urandom(256 * 1024),
        "data/numbers.csv": b"".join(f"{i},{i*i}\n".encode() for i in range(5000)),
        "unicode/élève – café.txt": "café ☕ naïve\n".encode(),
        "with spaces/a file.txt": b"spaces are fine\n",
    }
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(content)
    # An empty directory, which several formats handle badly.
    os.makedirs(os.path.join(root, "empty-folder"), exist_ok=True)
    return files


@pytest.fixture
def sample_tree(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    files = make_sample_tree(str(root))
    return str(root), files


def have(tool: str) -> bool:
    import shutil

    return shutil.which(tool) is not None


def run(*argv: str) -> subprocess.CompletedProcess:
    """Run a system tool, returning the completed process (never raises)."""
    return subprocess.run(argv, capture_output=True, text=True)
