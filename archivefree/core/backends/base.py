"""The interface every archive backend implements."""

from __future__ import annotations

import os
from collections.abc import Iterable

from ..entry import ArchiveEntry, ArchiveInfo
from ..errors import UnsafePath
from ..jobs import Progress


class Backend:
    """Read (and sometimes write) one family of archive formats.

    Subclasses must implement :meth:`list_entries` and :meth:`extract`. Anything
    they can't do — previewing, password handling — is answered by the default
    implementations here.

    Backends run on worker threads and must never import or touch GTK.
    """

    #: Format keys this backend claims, best-first.
    formats: tuple[str, ...] = ()
    #: Higher wins when several backends claim the same format.
    priority: int = 0

    def __init__(self, path: str, fmt: str, password: str | None = None):
        self.path = os.path.abspath(path)
        self.format = fmt
        self.password = password

    # -- capabilities ----------------------------------------------------
    @classmethod
    def is_available(cls) -> bool:
        """False when the backend's external tooling isn't installed."""
        return True

    @property
    def supports_selective_extract(self) -> bool:
        """True when extracting 3 files out of 10 000 doesn't unpack all 10 000."""
        return True

    @property
    def supports_preview(self) -> bool:
        return True

    # -- reading ---------------------------------------------------------
    def info(self) -> ArchiveInfo:
        raise NotImplementedError

    def list_entries(self, progress: Progress | None = None) -> list[ArchiveEntry]:
        raise NotImplementedError

    def extract(
        self,
        destination: str,
        entries: Iterable[ArchiveEntry] | None = None,
        progress: Progress | None = None,
        on_conflict=None,
        flatten: bool = False,
    ) -> list[str]:
        """Extract ``entries`` (or everything) into ``destination``.

        ``on_conflict`` is called as ``on_conflict(target_path, entry)`` when a
        file already exists and must return a
        :class:`~archivefree.core.conflict.Resolution`. Returns the list of
        paths actually written.
        """
        raise NotImplementedError

    def read_member(self, entry: ArchiveEntry, limit: int = 4 * 1024 * 1024) -> bytes:
        """Read up to ``limit`` bytes of one member, for previewing."""
        raise NotImplementedError

    def test(self, progress: Progress | None = None) -> list[str]:
        """Verify integrity. Returns a list of problem descriptions (empty = OK)."""
        return []

    def close(self) -> None:
        pass

    def __enter__(self) -> Backend:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- shared helpers --------------------------------------------------
    @staticmethod
    def safe_join(destination: str, member_path: str) -> str:
        """Join and verify a member path stays inside ``destination``.

        :func:`~archivefree.core.entry.normalise_path` already strips ``..`` on
        the way in; this is the second, authoritative check, done on the
        *resolved* path so that a symlink planted earlier in the same archive
        can't be used to redirect a later write outside the destination.
        """
        root = os.path.realpath(destination)
        target = os.path.realpath(os.path.join(root, member_path))
        if target != root and not target.startswith(root + os.sep):
            raise UnsafePath(member_path)
        return os.path.join(root, member_path)
