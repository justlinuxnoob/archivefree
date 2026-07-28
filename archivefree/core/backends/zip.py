"""ZIP via the standard library.

Using ``zipfile`` rather than shelling out to unzip means listing a 20 000-entry
archive is a single central-directory read with no process spawn — the browse
window opens instantly, which is the app's whole selling point.

The one thing stdlib can't do is AES (WinZip) encryption, which is what modern
tools produce. When we detect it, we hand the archive to the 7-Zip backend.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import tempfile
import zipfile
from collections.abc import Iterable

from ..conflict import Resolution, unique_path
from ..entry import ArchiveEntry, ArchiveInfo, normalise_path
from ..errors import CorruptArchive, PasswordRequired, WrongPassword
from ..jobs import Progress
from .base import Backend

#: Extra-field header IDs that mark WinZip AES encryption.
_AES_EXTRA_ID = 0x9901


class ZipBackend(Backend):
    formats = ("zip", "cbz", "epub", "jar", "apk", "ipa", "whl",
               "xpi", "ooxml", "odf")
    priority = 100

    def __init__(self, path: str, fmt: str, password: str | None = None):
        super().__init__(path, fmt, password)
        self._zf: zipfile.ZipFile | None = None
        self._entries: list[ArchiveEntry] | None = None

    # -- lifecycle -------------------------------------------------------
    def _open(self) -> zipfile.ZipFile:
        if self._zf is None:
            try:
                self._zf = zipfile.ZipFile(self.path, "r")
            except zipfile.BadZipFile as exc:
                raise CorruptArchive(
                    "This ZIP file is damaged and can’t be opened.", detail=str(exc)
                ) from exc
            except OSError as exc:
                raise CorruptArchive("Couldn’t read this file.", detail=str(exc)) from exc
        return self._zf

    def close(self) -> None:
        if self._zf is not None:
            self._zf.close()
            self._zf = None

    @staticmethod
    def uses_aes(path: str) -> bool:
        """True if any member uses AES encryption, which stdlib can't decrypt."""
        try:
            with zipfile.ZipFile(path) as zf:
                for zi in zf.infolist():
                    if zi.flag_bits & 0x1 and _has_aes_extra(zi):
                        return True
                    # Compression method 99 is the AES marker.
                    if zi.compress_type == 99:
                        return True
        except (zipfile.BadZipFile, OSError):
            return False
        return False

    # -- reading ---------------------------------------------------------
    def info(self) -> ArchiveInfo:
        zf = self._open()
        entries = self.list_entries()
        from .. import detect

        return ArchiveInfo(
            path=self.path,
            format=self.format,
            # A .cbz and a .docx are both ZIPs; say which one the user opened.
            format_label=detect.FORMATS[self.format].label,
            entry_count=sum(1 for e in entries if not e.is_dir),
            total_size=sum(e.size for e in entries if not e.is_dir),
            archive_size=os.path.getsize(self.path),
            encrypted=any(e.encrypted for e in entries),
            comment=zf.comment.decode("utf-8", "replace") if zf.comment else "",
        )

    def list_entries(self, progress: Progress | None = None) -> list[ArchiveEntry]:
        if self._entries is not None:
            return self._entries
        zf = self._open()
        out: list[ArchiveEntry] = []
        for zi in zf.infolist():
            if progress:
                progress.check()
            path = normalise_path(zi.filename)
            if not path:
                continue
            out.append(
                ArchiveEntry(
                    path=path,
                    size=zi.file_size,
                    compressed_size=zi.compress_size,
                    modified=_zip_date(zi),
                    is_dir=zi.is_dir(),
                    is_symlink=_is_symlink(zi),
                    encrypted=bool(zi.flag_bits & 0x1),
                    crc=f"{zi.CRC:08X}" if not zi.is_dir() else None,
                    token=zi,
                )
            )
        self._entries = out
        return out

    def read_member(self, entry: ArchiveEntry, limit: int = 4 * 1024 * 1024) -> bytes:
        zf = self._open()
        zi = entry.token if isinstance(entry.token, zipfile.ZipInfo) else entry.path
        pwd = self.password.encode() if self.password else None
        try:
            with zf.open(zi, "r", pwd=pwd) as fh:
                return fh.read(limit)
        except RuntimeError as exc:
            raise _password_error(exc, entry) from exc
        except zipfile.BadZipFile as exc:
            raise CorruptArchive(
                f"“{entry.name}” is damaged and couldn’t be read.", detail=str(exc)
            ) from exc

    # -- extracting ------------------------------------------------------
    def extract(
        self,
        destination: str,
        entries: Iterable[ArchiveEntry] | None = None,
        progress: Progress | None = None,
        on_conflict=None,
        flatten: bool = False,
    ) -> list[str]:
        zf = self._open()
        members = list(entries) if entries is not None else self.list_entries()
        members = [m for m in members if not m.is_dir]
        pwd = self.password.encode() if self.password else None

        total = sum(m.size for m in members) or len(members) or 1
        if progress:
            progress.begin(total, "Preparing…")

        written: list[str] = []
        done = 0
        os.makedirs(destination, exist_ok=True)

        for member in members:
            if progress:
                progress.check()
                progress.set_message(member.name)

            rel = member.name if flatten else member.path
            target = self.safe_join(destination, rel)

            if os.path.exists(target):
                resolution = on_conflict(target, member) if on_conflict else Resolution.RENAME
                if resolution is Resolution.CANCEL:
                    from ..jobs import Cancelled

                    raise Cancelled()
                if resolution is Resolution.SKIP:
                    done += member.size
                    if progress:
                        progress.current = done
                        progress._emit()
                    continue
                if resolution is Resolution.RENAME:
                    target = unique_path(target)

            os.makedirs(os.path.dirname(target) or destination, exist_ok=True)
            zi = member.token if isinstance(member.token, zipfile.ZipInfo) else member.path

            try:
                if member.is_symlink:
                    with zf.open(zi, "r", pwd=pwd) as fh:
                        link_target = fh.read(4096).decode("utf-8", "replace")
                    _write_symlink(destination, target, link_target)
                else:
                    with zf.open(zi, "r", pwd=pwd) as src, open(target, "wb") as dst:
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
            except RuntimeError as exc:
                raise _password_error(exc, member) from exc
            except zipfile.BadZipFile as exc:
                raise CorruptArchive(
                    f"“{member.name}” is damaged, so extraction stopped.", detail=str(exc)
                ) from exc
            except OSError as exc:
                raise _os_error(exc, destination) from exc

            _apply_metadata(target, member, zi)
            written.append(target)

        if progress:
            progress.current = progress.total
            progress.set_message("Finished")
        return written

    # -- modifying ---------------------------------------------------------
    @property
    def supports_modification(self) -> bool:
        # Encrypted archives would need the password to re-encrypt every entry
        # we copy across, which the stdlib cannot do.
        return not any(e.encrypted for e in self.list_entries())

    def add(self, sources, into: str = "", progress: Progress | None = None) -> int:
        """Append files to the archive.

        Written into a copy and swapped in atomically rather than appended in
        place: appending to the real file leaves it truncated and unreadable if
        anything fails halfway, and this is a file the user already had.
        """
        pairs = _walk_sources(sources, into)
        if not pairs:
            return 0

        existing = {e.path for e in self.list_entries()}
        total = sum(_size_of(p) for p, _ in pairs) or 1
        if progress:
            progress.begin(total, "Adding…")

        done = 0
        with _rewritten(self.path) as temp, \
                zipfile.ZipFile(self.path, "r") as source_zip, \
                zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED,
                                allowZip64=True) as target:
                target.comment = source_zip.comment
                replaced = {arc for _, arc in pairs}
                for item in source_zip.infolist():
                    if progress:
                        progress.check()
                    if normalise_path(item.filename) in replaced:
                        continue  # the new copy wins
                    target.writestr(item, source_zip.read(item.filename))
                for full, arcname in pairs:
                    if progress:
                        progress.check()
                        progress.set_message(os.path.basename(full))
                    target.write(full, arcname)
                    done += _size_of(full)
                    if progress:
                        progress.current = min(done, total)
                        progress._emit()

        self._invalidate()
        return len([a for _, a in pairs if a not in existing])

    def delete(self, entries, progress: Progress | None = None) -> int:
        """Remove entries by rewriting the archive without them."""
        doomed = set()
        for entry in entries:
            doomed.add(entry.path)
            if entry.is_dir:
                doomed.update(e.path for e in self.list_entries()
                              if e.path.startswith(entry.path + "/"))
        if not doomed:
            return 0

        keep = [e for e in self.list_entries() if e.path not in doomed]
        if progress:
            progress.begin(len(keep) or 1, "Removing…")

        removed = 0
        with _rewritten(self.path) as temp, \
                zipfile.ZipFile(self.path, "r") as source_zip, \
                zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED,
                                allowZip64=True) as target:
                target.comment = source_zip.comment
                for item in source_zip.infolist():
                    if progress:
                        progress.check()
                        progress.step(1)
                    if normalise_path(item.filename) in doomed:
                        removed += 1
                        continue
                    target.writestr(item, source_zip.read(item.filename))

        self._invalidate()
        return removed

    def test(self, progress: Progress | None = None) -> list[str]:
        zf = self._open()
        entries = [e for e in self.list_entries() if not e.is_dir]
        if progress:
            progress.begin(len(entries), "Checking…")
        problems: list[str] = []
        pwd = self.password.encode() if self.password else None
        for e in entries:
            if progress:
                progress.step(1, e.name)
            try:
                with zf.open(e.token, "r", pwd=pwd) as fh:
                    while fh.read(1024 * 256):
                        if progress:
                            progress.check()
            except Exception as exc:
                problems.append(f"{e.path}: {exc}")
        return problems


# -- helpers -------------------------------------------------------------


def _has_aes_extra(zi: zipfile.ZipInfo) -> bool:
    """Scan the extra field for the 0x9901 (AES) header."""
    data = zi.extra
    i = 0
    while i + 4 <= len(data):
        header_id = int.from_bytes(data[i : i + 2], "little")
        size = int.from_bytes(data[i + 2 : i + 4], "little")
        if header_id == _AES_EXTRA_ID:
            return True
        i += 4 + size
    return False


def _zip_date(zi: zipfile.ZipInfo) -> datetime.datetime | None:
    try:
        return datetime.datetime(*zi.date_time)
    except (ValueError, TypeError):
        return None


def _is_symlink(zi: zipfile.ZipInfo) -> bool:
    # Unix mode lives in the top 16 bits of external_attr; 0xA000 == S_IFLNK.
    return (zi.external_attr >> 16) & 0xF000 == 0xA000


def _write_symlink(destination: str, target: str, link_target: str) -> None:
    """Create a symlink, whatever it points at.

    See the note in the tar backend: the link itself is inert, and writes
    *through* a link are what must be blocked — which ``safe_join`` already
    does by resolving every write against the destination root.
    """
    if os.path.lexists(target):
        os.unlink(target)
    os.symlink(link_target, target)


def _apply_metadata(target: str, entry: ArchiveEntry, zi) -> None:
    """Restore mtime and the executable bit, quietly skipping if unsupported."""
    if entry.is_symlink:
        return
    try:
        if entry.modified:
            ts = entry.modified.timestamp()
            os.utime(target, (ts, ts))
    except (OSError, OverflowError, ValueError):
        pass
    try:
        mode = (zi.external_attr >> 16) & 0o7777
        if mode and (mode & 0o111):
            os.chmod(target, (os.stat(target).st_mode | 0o111) & 0o7777)
    except (OSError, AttributeError):
        pass


def _password_error(exc: RuntimeError, entry: ArchiveEntry) -> Exception:
    text = str(exc).lower()
    if "password required" in text:
        return PasswordRequired()
    if "bad password" in text or "password" in text:
        return WrongPassword()
    return CorruptArchive(f"“{entry.name}” couldn’t be read.", detail=str(exc))


def _os_error(exc: OSError, destination: str) -> Exception:
    import errno

    from ..errors import ArchiveError, DiskFull

    if exc.errno == errno.ENOSPC:
        return DiskFull(destination)
    if exc.errno in (errno.EACCES, errno.EPERM):
        return ArchiveError(
            "ArchiveFree doesn’t have permission to write there.",
            detail=str(exc),
            hint="Choose a different folder, such as your Home or Downloads folder.",
        )
    if exc.errno == errno.EROFS:
        return ArchiveError(
            "That location is read-only.",
            detail=str(exc),
            hint="Choose a folder you can write to.",
        )
    if exc.errno == errno.ENAMETOOLONG:
        return ArchiveError(
            "One of the file names in this archive is too long for this filesystem.",
            detail=str(exc),
        )
    return ArchiveError("Extraction failed.", detail=str(exc))


def _walk_sources(sources, into: str) -> list[tuple[str, str]]:
    """Expand files and folders into (path on disk, name inside the archive)."""
    import posixpath

    pairs: list[tuple[str, str]] = []
    for source in sources:
        source = os.path.abspath(source)
        base = os.path.dirname(source)
        if os.path.isdir(source):
            for dirpath, dirnames, filenames in os.walk(source):
                dirnames.sort()
                filenames.sort()
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    pairs.append((full, posixpath.join(into, rel) if into else rel))
        else:
            rel = os.path.basename(source)
            pairs.append((source, posixpath.join(into, rel) if into else rel))
    return pairs


def _size_of(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


@contextlib.contextmanager
def _rewritten(path: str):
    """Build a replacement archive beside ``path`` and swap it in atomically.

    Modifying an archive means rewriting a file the user already has, so the
    original is never touched until the replacement is complete on disk. If
    anything raises — including a cancellation — the temporary file is removed
    and the original is left exactly as it was.

    The temporary lives in the same directory so ``os.replace`` is an atomic
    rename rather than a copy across filesystems.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temp = tempfile.mkstemp(prefix=".archivefree-", suffix=".tmp",
                                    dir=directory)
    os.close(handle)
    try:
        yield temp
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    else:
        # Carry the original's permissions over; mkstemp creates 0600.
        try:
            os.chmod(temp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        os.replace(temp, path)
