"""The data model shared by every backend."""

from __future__ import annotations

import datetime
import posixpath
from dataclasses import dataclass, field


@dataclass(slots=True)
class ArchiveEntry:
    """One member of an archive.

    ``path`` is always normalised to forward slashes, relative, with no leading
    "./" and no trailing slash — directories are flagged by ``is_dir`` instead.
    Backends must not put absolute or "../" paths here; :func:`normalise_path`
    handles that.
    """

    path: str
    size: int = 0
    compressed_size: int = 0
    modified: datetime.datetime | None = None
    is_dir: bool = False
    is_symlink: bool = False
    encrypted: bool = False
    crc: str | None = None
    # Backend-private handle used to extract this entry without re-scanning.
    token: object | None = None

    @property
    def name(self) -> str:
        return posixpath.basename(self.path) or self.path

    @property
    def parent(self) -> str:
        return posixpath.dirname(self.path)

    @property
    def extension(self) -> str:
        _, _, ext = self.name.rpartition(".")
        return ext.lower() if ext and ext != self.name else ""


@dataclass(slots=True)
class ArchiveInfo:
    """Summary of an opened archive, shown in the header and properties view."""

    path: str
    format: str
    format_label: str
    entry_count: int = 0
    total_size: int = 0
    archive_size: int = 0
    encrypted: bool = False
    # Names of every volume for a split archive; empty for single-file archives.
    volumes: list[str] = field(default_factory=list)
    comment: str = ""

    @property
    def ratio(self) -> float:
        if not self.total_size:
            return 0.0
        return 1.0 - (self.archive_size / self.total_size)


def normalise_path(raw: str) -> str:
    """Make an archive member path safe and canonical.

    Strips drive letters, leading slashes and any ``..`` segments, so a
    malicious archive cannot describe a path outside the extraction root. The
    caller still verifies the final resolved path (defence in depth), but doing
    it here means the browse view never *displays* a bogus path either.
    """
    p = raw.replace("\\", "/")
    # Windows drive letter, e.g. "C:/foo"
    if len(p) > 1 and p[1] == ":":
        p = p[2:]
    parts: list[str] = []
    for seg in p.split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)
