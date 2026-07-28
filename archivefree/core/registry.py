"""Choosing a backend for a file.

Selection is by format key, then by backend priority, then by availability. The
stdlib backends outrank the CLI ones so the common cases (zip, tar) never spawn
a process; 7-Zip picks up everything else.
"""

from __future__ import annotations

import os

from . import detect
from .backends.base import Backend
from .backends.sevenzip import SevenZipBackend
from .backends.single import SingleStreamBackend
from .backends.tar import TarBackend
from .backends.unrar import UnrarBackend
from .backends.zip import ZipBackend
from .errors import MissingTool, UnsupportedFormat

_BACKENDS: list[type[Backend]] = [
    ZipBackend,
    TarBackend,
    SingleStreamBackend,
    UnrarBackend,
    SevenZipBackend,
]


def backends_for(fmt: str) -> list[type[Backend]]:
    """Every backend that claims ``fmt``, best-first."""
    candidates = [b for b in _BACKENDS if fmt in b.formats]
    return sorted(candidates, key=lambda b: -b.priority)


def open_archive(path: str, password: str | None = None,
                 fmt: str | None = None) -> Backend:
    """Open ``path`` with the best available backend.

    Raises :class:`UnsupportedFormat` if we don't recognise the file, or
    :class:`MissingTool` when we recognise it but the tool that reads it isn't
    installed — the two cases need very different advice, so they stay distinct.
    """
    if not os.path.exists(path):
        from .errors import ArchiveError

        raise ArchiveError(
            f"“{os.path.basename(path)}” doesn’t exist.",
            hint="It may have been moved or deleted.",
        )

    # Any part of a split set opens the set as a whole.
    path = detect.first_volume(path)
    fmt = fmt or detect.detect_format(path)
    if fmt is None:
        raise UnsupportedFormat(
            f"“{os.path.basename(path)}” doesn’t look like an archive ArchiveFree can open.",
            hint="It may be a plain file, or a format ArchiveFree doesn’t support yet.",
        )

    candidates = backends_for(fmt)
    if not candidates:
        raise UnsupportedFormat(
            f"ArchiveFree can’t open {detect.FORMATS[fmt].label} files yet."
        )

    # A ZIP using AES encryption is beyond stdlib, so hand it to 7-Zip.
    if fmt == "zip" and ZipBackend.uses_aes(path):
        candidates = [b for b in candidates if b is not ZipBackend] or candidates

    available = [b for b in candidates if b.is_available()]
    if not available:
        label = detect.FORMATS[fmt].label
        raise MissingTool("7z", f"open {label} files", "7zip")

    return available[0](path, fmt, password)


def probe(path: str) -> tuple[str | None, bool]:
    """(format key, can we open it?) — used to decide whether to show an error."""
    fmt = detect.detect_format(path)
    if fmt is None:
        return None, False
    return fmt, any(b.is_available() for b in backends_for(fmt))
