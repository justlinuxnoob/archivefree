"""TAR and its compressed variants, via the standard library.

Tar is a sequential format: there is no index, so listing means walking the
whole stream. For ``.tar.gz`` and friends that also means decompressing it. We
therefore scan exactly once, cache the result, and — crucially — extract in a
single sequential pass no matter how many members were selected, instead of
re-seeking per file the way a naive implementation would.

Compression codecs come from the stdlib where possible (gzip/bz2/lzma). Zstd
and LZ4 aren't in Python 3.13's stdlib, so those stream through the ``zstd`` /
``lz4`` binaries via a pipe, which is just as fast and avoids a C extension we
would otherwise have to build and ship.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import subprocess
import tarfile
from collections.abc import Iterable

from .. import tools
from ..conflict import Resolution, unique_path
from ..entry import ArchiveEntry, ArchiveInfo, normalise_path
from ..errors import ArchiveError, CorruptArchive
from ..jobs import Cancelled, Progress
from .base import Backend
from .zip import _os_error

#: format key -> (stdlib opener or None, external binary, decompress args)
_CODECS = {
    "tar": (None, None, None),
    "tar.gz": (gzip.open, None, None),
    "tar.bz2": (bz2.open, None, None),
    "tar.xz": (lzma.open, None, None),
    "tar.lzma": (lzma.open, None, None),
    "tar.zst": (None, "zstd", ["-dc"]),
    "tar.lz4": (None, "lz4", ["-dc"]),
}

_LABELS = {
    "tar": "TAR archive",
    "tar.gz": "Gzip-compressed TAR",
    "tar.bz2": "Bzip2-compressed TAR",
    "tar.xz": "XZ-compressed TAR",
    "tar.lzma": "LZMA-compressed TAR",
    "tar.zst": "Zstandard-compressed TAR",
    "tar.lz4": "LZ4-compressed TAR",
}


class _PipeStream(io.RawIOBase):
    """Read-only stream fed by an external decompressor's stdout.

    ``tarfile`` in stream mode ("r|") only ever reads forward, which is exactly
    what a pipe supports, so this is enough to make zstd and lz4 work without a
    Python binding for either.
    """

    def __init__(self, argv: list[str], path: str):
        self._proc = subprocess.Popen(
            [*argv, path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1024 * 256,
        )
        self._stdout = self._proc.stdout

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:  # type: ignore[override]
        assert self._stdout is not None
        data = self._stdout.read(len(buffer))
        if not data:
            return 0
        buffer[: len(data)] = data
        return len(data)

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
        super().close()


class TarBackend(Backend):
    formats = ("tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst", "tar.lz4", "tar.lzma")
    priority = 100

    def __init__(self, path: str, fmt: str, password: str | None = None):
        super().__init__(path, fmt, password)
        self._entries: list[ArchiveEntry] | None = None

    @property
    def supports_selective_extract(self) -> bool:
        return True  # implemented as a filtered single pass

    @classmethod
    def is_available(cls) -> bool:
        return True

    def _open_tar(self) -> tarfile.TarFile:
        """Open the archive for a forward-only pass."""
        stdlib_open, binary, args = _CODECS[self.format]
        try:
            if binary:
                exe = tools.require(binary, f"open {_LABELS[self.format]} files")
                stream = _PipeStream([exe] + (args or []), self.path)
                return tarfile.open(fileobj=stream, mode="r|")
            if stdlib_open:
                return tarfile.open(fileobj=stdlib_open(self.path, "rb"), mode="r|")
            return tarfile.open(self.path, mode="r|")
        except tarfile.ReadError as exc:
            raise CorruptArchive(
                "This archive is damaged, or isn’t the kind of file it claims to be.",
                detail=str(exc),
            ) from exc
        except (EOFError, OSError) as exc:
            raise CorruptArchive(
                "This archive looks incomplete — it may not have finished downloading.",
                detail=str(exc),
            ) from exc

    # -- reading ---------------------------------------------------------
    def info(self) -> ArchiveInfo:
        entries = self.list_entries()
        return ArchiveInfo(
            path=self.path,
            format=self.format,
            format_label=_LABELS.get(self.format, "TAR archive"),
            entry_count=sum(1 for e in entries if not e.is_dir),
            total_size=sum(e.size for e in entries if not e.is_dir),
            archive_size=os.path.getsize(self.path),
        )

    def list_entries(self, progress: Progress | None = None) -> list[ArchiveEntry]:
        if self._entries is not None:
            return self._entries
        out: list[ArchiveEntry] = []
        if progress:
            progress.begin(0, "Reading archive…")
        tf = self._open_tar()
        try:
            for member in tf:
                if progress:
                    progress.check()
                path = normalise_path(member.name)
                if not path:
                    continue
                out.append(
                    ArchiveEntry(
                        path=path,
                        size=member.size,
                        compressed_size=0,  # tar members have no per-file compressed size
                        modified=_mtime(member),
                        is_dir=member.isdir(),
                        is_symlink=member.issym() or member.islnk(),
                        token=path,
                    )
                )
                if progress and len(out) % 256 == 0:
                    progress.set_message(f"Reading archive… {len(out):,} items")
        except tarfile.ReadError as exc:
            if not out:
                raise CorruptArchive(
                    "This archive is damaged and couldn’t be read.", detail=str(exc)
                ) from exc
            # Truncated archive: keep what we managed to read, flag it below.
        finally:
            tf.close()
        self._entries = out
        return out

    def read_member(self, entry: ArchiveEntry, limit: int = 4 * 1024 * 1024) -> bytes:
        tf = self._open_tar()
        try:
            for member in tf:
                if normalise_path(member.name) == entry.path:
                    fh = tf.extractfile(member)
                    return fh.read(limit) if fh else b""
        finally:
            tf.close()
        raise ArchiveError(f"“{entry.name}” is no longer in this archive.")

    # -- extracting ------------------------------------------------------
    def extract(
        self,
        destination: str,
        entries: Iterable[ArchiveEntry] | None = None,
        progress: Progress | None = None,
        on_conflict=None,
        flatten: bool = False,
    ) -> list[str]:
        wanted: set[str] | None = None
        if entries is not None:
            wanted = set()
            for e in entries:
                wanted.add(e.path)
                if e.is_dir:
                    # Selecting a folder means selecting everything under it.
                    prefix = e.path + "/"
                    wanted.update(
                        x.path for x in self.list_entries() if x.path.startswith(prefix)
                    )

        all_entries = self.list_entries()
        selected = [e for e in all_entries if wanted is None or e.path in wanted]
        total = sum(e.size for e in selected if not e.is_dir) or len(selected) or 1
        if progress:
            progress.begin(total, "Preparing…")

        os.makedirs(destination, exist_ok=True)
        written: list[str] = []
        done = 0

        tf = self._open_tar()
        try:
            for member in tf:
                if progress:
                    progress.check()
                path = normalise_path(member.name)
                if not path or (wanted is not None and path not in wanted):
                    continue

                rel = os.path.basename(path) if flatten else path
                target = self.safe_join(destination, rel)

                if member.isdir():
                    if not flatten:
                        os.makedirs(target, exist_ok=True)
                    continue

                if progress:
                    progress.set_message(os.path.basename(path))

                if os.path.lexists(target):
                    stub = _entry_for(all_entries, path)
                    resolution = on_conflict(target, stub) if on_conflict else Resolution.RENAME
                    if resolution is Resolution.CANCEL:
                        raise Cancelled()
                    if resolution is Resolution.SKIP:
                        done += member.size
                        if progress:
                            progress.current = done
                            progress._emit()
                        continue
                    if resolution is Resolution.RENAME:
                        target = unique_path(target)
                    elif os.path.lexists(target):
                        _remove(target)

                os.makedirs(os.path.dirname(target) or destination, exist_ok=True)

                try:
                    if member.issym():
                        _safe_symlink(destination, target, member.linkname)
                        written.append(target)
                        continue
                    if member.islnk():
                        source = self.safe_join(destination, normalise_path(member.linkname))
                        if os.path.exists(source):
                            if os.path.lexists(target):
                                _remove(target)
                            os.link(source, target)
                            written.append(target)
                        continue
                    if not member.isfile():
                        continue  # devices, fifos: not extracted, by design

                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with open(target, "wb") as dst:
                        while True:
                            if progress:
                                progress.check()
                            chunk = src.read(1024 * 256)
                            if not chunk:
                                break
                            dst.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress.current = done
                                progress._emit()
                except OSError as exc:
                    raise _os_error(exc, destination) from exc

                _restore(target, member)
                written.append(target)
        except tarfile.ReadError as exc:
            raise CorruptArchive(
                "This archive is damaged — extraction stopped partway through.",
                detail=str(exc),
            ) from exc
        finally:
            tf.close()

        if progress:
            progress.current = progress.total
            progress.set_message("Finished")
        return written

    def test(self, progress: Progress | None = None) -> list[str]:
        problems: list[str] = []
        tf = self._open_tar()
        try:
            entries = self.list_entries()
            if progress:
                progress.begin(len(entries) or 1, "Checking…")
            for member in tf:
                if progress:
                    progress.step(1, member.name)
                if not member.isfile():
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                try:
                    while fh.read(1024 * 256):
                        if progress:
                            progress.check()
                except Exception as exc:
                    problems.append(f"{member.name}: {exc}")
        except tarfile.ReadError as exc:
            problems.append(f"Archive is truncated or damaged: {exc}")
        finally:
            tf.close()
        return problems


# -- helpers -------------------------------------------------------------


def _mtime(member: tarfile.TarInfo):
    import datetime

    try:
        return datetime.datetime.fromtimestamp(member.mtime)
    except (OverflowError, OSError, ValueError):
        return None


def _entry_for(entries: list[ArchiveEntry], path: str) -> ArchiveEntry:
    for e in entries:
        if e.path == path:
            return e
    return ArchiveEntry(path=path)


def _remove(path: str) -> None:
    import shutil

    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _safe_symlink(destination: str, target: str, link_target: str) -> None:
    """Create a symlink, whatever it points at.

    Refusing links that point outside the destination sounds safe but is wrong,
    and it broke every root filesystem tarball and container layer: those are
    *full* of absolute links like ``/bin/sh -> /bin/busybox``, which is simply
    how a rootfs is built. GNU tar and every container runtime create them.

    A symlink is inert on its own — it is just a name. The attack is a *later*
    archive member writing *through* it to escape the destination, and that is
    blocked independently: :meth:`Backend.safe_join` resolves the real path of
    every write, so an entry like ``link/passwd`` where ``link -> /etc``
    resolves outside the root and is refused there. Guarding the link itself
    adds nothing and costs correctness.
    """
    if os.path.lexists(target):
        _remove(target)
    os.symlink(link_target, target)


def _restore(target: str, member: tarfile.TarInfo) -> None:
    """Restore mode and mtime. Never restores setuid/setgid bits."""
    try:
        os.chmod(target, member.mode & 0o777)
    except OSError:
        pass
    try:
        os.utime(target, (member.mtime, member.mtime))
    except (OSError, OverflowError, ValueError):
        pass
