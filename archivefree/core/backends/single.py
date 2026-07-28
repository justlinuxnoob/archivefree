"""Plain single-stream compressed files: .gz, .bz2, .xz, .zst, .lz4, .lzma.

These have no filename table — a ``.gz`` is just one compressed blob. We
synthesise a single entry named after the archive with its suffix removed, so
``notes.txt.gz`` browses as a one-item archive containing ``notes.txt``. That's
a small fiction, but it means the browse-then-extract flow works identically
for every format instead of special-casing these in the UI.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import subprocess
from collections.abc import Iterable

from .. import detect, tools
from ..conflict import Resolution, unique_path
from ..entry import ArchiveEntry, ArchiveInfo
from ..errors import CorruptArchive
from ..jobs import Cancelled, Progress
from .base import Backend
from .zip import _os_error

_STDLIB = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open, "lzma": lzma.open}
_EXTERNAL = {"zst": ("zstd", ["-dc"]), "lz4": ("lz4", ["-dc"]), "z": ("gzip", ["-dc"])}
_SUFFIXES = {"gz": ".gz", "bz2": ".bz2", "xz": ".xz", "zst": ".zst",
             "lz4": ".lz4", "lzma": ".lzma", "z": ".Z"}


class SingleStreamBackend(Backend):
    formats = ("gz", "bz2", "xz", "zst", "lz4", "lzma", "z")
    priority = 100

    @property
    def supports_selective_extract(self) -> bool:
        return False  # there's only ever one member

    def _inner_name(self) -> str:
        name = os.path.basename(self.path)
        suffix = _SUFFIXES.get(self.format, "")
        for candidate in (suffix, suffix.upper()):
            if candidate and name.lower().endswith(candidate.lower()):
                return name[: -len(candidate)] or "data"
        return name + ".out"

    def _open_stream(self):
        if self.format in _STDLIB:
            return _STDLIB[self.format](self.path, "rb")
        binary, args = _EXTERNAL[self.format]
        exe = tools.require(binary, f"open .{self.format} files")
        proc = subprocess.Popen(
            [exe, *args, self.path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1024 * 256,
        )
        return _ProcStream(proc)

    # -- reading ---------------------------------------------------------
    def info(self) -> ArchiveInfo:
        entries = self.list_entries()
        return ArchiveInfo(
            path=self.path,
            format=self.format,
            format_label=detect.FORMATS[self.format].label,
            entry_count=1,
            total_size=entries[0].size,
            archive_size=os.path.getsize(self.path),
        )

    def list_entries(self, progress: Progress | None = None) -> list[ArchiveEntry]:
        import datetime

        stat = os.stat(self.path)
        size = detect.read_uncompressed_size(self.path, self.format)
        return [
            ArchiveEntry(
                path=self._inner_name(),
                # -1 means "unknown"; the UI shows "—" rather than a wrong 0.
                size=size if size is not None else -1,
                compressed_size=stat.st_size,
                modified=datetime.datetime.fromtimestamp(stat.st_mtime),
                is_dir=False,
                token=None,
            )
        ]

    def read_member(self, entry: ArchiveEntry, limit: int = 4 * 1024 * 1024) -> bytes:
        stream = self._open_stream()
        try:
            return stream.read(limit)
        except (OSError, EOFError, gzip.BadGzipFile, lzma.LZMAError) as exc:
            raise CorruptArchive("This compressed file is damaged.", detail=str(exc)) from exc
        finally:
            stream.close()

    # -- extracting ------------------------------------------------------
    def extract(
        self,
        destination: str,
        entries: Iterable[ArchiveEntry] | None = None,
        progress: Progress | None = None,
        on_conflict=None,
        flatten: bool = False,
    ) -> list[str]:
        entry = self.list_entries()[0]
        target = self.safe_join(destination, entry.name)
        os.makedirs(destination, exist_ok=True)

        if os.path.exists(target):
            resolution = on_conflict(target, entry) if on_conflict else Resolution.RENAME
            if resolution is Resolution.CANCEL:
                raise Cancelled()
            if resolution is Resolution.SKIP:
                return []
            if resolution is Resolution.RENAME:
                target = unique_path(target)

        # Compressed size is the only progress signal we have when the
        # uncompressed size isn't recorded, so scale by the ratio we observe.
        total = entry.size if entry.size > 0 else os.path.getsize(self.path) * 3
        if progress:
            progress.begin(max(total, 1), entry.name)

        stream = self._open_stream()
        written = 0
        try:
            with open(target, "wb") as dst:
                while True:
                    if progress:
                        progress.check()
                    chunk = stream.read(1024 * 256)
                    if not chunk:
                        break
                    dst.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress.current = min(written, progress.total)
                        progress._emit()
        except (gzip.BadGzipFile, lzma.LZMAError, EOFError) as exc:
            raise CorruptArchive("This compressed file is damaged.", detail=str(exc)) from exc
        except OSError as exc:
            raise _os_error(exc, destination) from exc
        finally:
            stream.close()

        if progress:
            progress.current = progress.total
            progress.set_message("Finished")
        return [target]

    def test(self, progress: Progress | None = None) -> list[str]:
        if progress:
            progress.begin(1, "Checking…")
        stream = self._open_stream()
        try:
            while stream.read(1024 * 256):
                if progress:
                    progress.check()
        except Exception as exc:
            return [f"{os.path.basename(self.path)}: {exc}"]
        finally:
            stream.close()
        return []


class _ProcStream:
    """Minimal read/close wrapper over a decompressor subprocess."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc

    def read(self, size: int = -1) -> bytes:
        assert self._proc.stdout is not None
        return self._proc.stdout.read(size)

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for pipe in (self._proc.stdout, self._proc.stderr):
            if pipe:
                pipe.close()
