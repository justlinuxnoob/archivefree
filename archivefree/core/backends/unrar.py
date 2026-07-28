"""RAR via the ``unrar`` command-line tool.

7-Zip already reads RAR, so this exists purely as a fallback: Debian ships
``unrar`` in non-free while ``7zip`` is a separate package, and plenty of
machines have one but not the other. It's also slightly better at solid and
recovery-record archives, so it wins when both are present.

It cannot be shipped in the Flatpak (non-free), which is why 7-Zip remains the
backend the packaged build relies on.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
from collections.abc import Iterable

from .. import tools
from ..conflict import Resolution
from ..entry import ArchiveEntry, ArchiveInfo, normalise_path
from ..errors import (
    ArchiveError,
    CorruptArchive,
    MissingVolume,
    PasswordRequired,
    WrongPassword,
)
from ..jobs import Cancelled, Progress
from .base import Backend
from .sevenzip import _expand_folders, _kill

_PERCENT_RE = re.compile(rb"(\d{1,3})%")


class UnrarBackend(Backend):
    formats = ("rar", "cbr")
    priority = 50  # above SevenZipBackend (10), below the stdlib backends

    def __init__(self, path: str, fmt: str, password: str | None = None):
        super().__init__(path, fmt, password)
        self._entries: list[ArchiveEntry] | None = None
        self._encrypted = False

    @classmethod
    def is_available(cls) -> bool:
        return tools.which("unrar") is not None

    def _exe(self) -> str:
        return tools.require("unrar", "open RAR archives")

    def _password_args(self) -> list[str]:
        # "-p-" means "no password"; without it unrar blocks on a terminal prompt.
        return [f"-p{self.password}"] if self.password else ["-p-"]

    def _translate(self, code: int, stderr: str) -> ArchiveError:
        low = stderr.lower()
        if "wrong password" in low or "incorrect password" in low or code == 11:
            return WrongPassword() if self.password else PasswordRequired()
        if "encrypted" in low and "password" in low:
            return PasswordRequired()
        if "cannot find volume" in low:
            match = re.search(r"cannot find volume\s+(\S+)", stderr, re.I)
            return MissingVolume(
                os.path.basename(match.group(1)) if match else "a later part",
                detail=stderr.strip(),
            )
        if "checksum error" in low or "crc failed" in low or code == 3:
            return CorruptArchive(
                "This RAR archive is damaged — some files failed their integrity check.",
                detail=stderr.strip(),
            )
        if "is not rar archive" in low:
            from ..errors import UnsupportedFormat

            return UnsupportedFormat("This isn’t a RAR archive.", detail=stderr.strip())
        if code == 5:
            return ArchiveError(
                "ArchiveFree doesn’t have permission to write there.",
                detail=stderr.strip(),
                hint="Choose a different destination folder.",
            )
        return ArchiveError("The RAR tool reported a problem.",
                            detail=stderr.strip() or f"unrar exited with status {code}")

    # -- reading ---------------------------------------------------------
    def info(self) -> ArchiveInfo:
        from .. import detect

        entries = self.list_entries()
        volumes = detect.split_volumes(self.path)
        return ArchiveInfo(
            path=self.path,
            format=self.format,
            format_label=detect.FORMATS[self.format].label,
            entry_count=sum(1 for e in entries if not e.is_dir),
            total_size=sum(e.size for e in entries if not e.is_dir),
            archive_size=sum(os.path.getsize(v) for v in volumes) if volumes
            else os.path.getsize(self.path),
            encrypted=self._encrypted,
            volumes=volumes,
        )

    def list_entries(self, progress: Progress | None = None) -> list[ArchiveEntry]:
        if self._entries is not None:
            return self._entries
        argv = [self._exe(), "lt", "-idc", *self._password_args(), self.path]
        proc = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise self._translate(
                proc.returncode,
                (proc.stderr or proc.stdout).decode("utf-8", "replace"),
            )
        self._entries = self._parse_lt(proc.stdout.decode("utf-8", "replace"))
        return self._entries

    def _parse_lt(self, text: str) -> list[ArchiveEntry]:
        """Parse ``unrar lt`` (technical list) output."""
        entries: list[ArchiveEntry] = []
        fields: dict[str, str] = {}

        def flush() -> None:
            raw = fields.get("Name")
            if not raw:
                return
            path = normalise_path(raw)
            if not path:
                return
            attrs = fields.get("Attributes", "")
            flags = fields.get("Flags", "")
            encrypted = "encrypted" in flags.lower()
            if encrypted:
                self._encrypted = True
            entries.append(
                ArchiveEntry(
                    path=path,
                    size=_int(fields.get("Size")),
                    compressed_size=_int(fields.get("Packed size")),
                    modified=_timestamp(fields.get("mtime")),
                    is_dir=fields.get("Type", "").lower() == "directory"
                    or attrs.startswith("d") or attrs.startswith("D"),
                    is_symlink=fields.get("Type", "").lower() == "link",
                    encrypted=encrypted,
                    crc=fields.get("CRC32") or None,
                    token=raw,
                )
            )

        for line in text.splitlines():
            if not line.strip():
                if fields:
                    flush()
                    fields = {}
                continue
            key, sep, value = line.partition(": ")
            if sep:
                fields[key.strip()] = value.strip()
        if fields:
            flush()
        return entries

    def read_member(self, entry: ArchiveEntry, limit: int = 4 * 1024 * 1024) -> bytes:
        argv = [self._exe(), "p", "-idq", "-inul", *self._password_args(),
                self.path, entry.path]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
        try:
            assert proc.stdout is not None
            return proc.stdout.read(limit)
        finally:
            _kill(proc)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    # -- extracting ------------------------------------------------------
    def extract(
        self,
        destination: str,
        entries: Iterable[ArchiveEntry] | None = None,
        progress: Progress | None = None,
        on_conflict=None,
        flatten: bool = False,
    ) -> list[str]:
        all_entries = self.list_entries()
        selected = _expand_folders(list(entries), all_entries) if entries is not None \
            else all_entries
        files = [e for e in selected if not e.is_dir]
        if not files:
            return []

        os.makedirs(destination, exist_ok=True)
        if progress:
            progress.begin(1000, "Preparing…")

        # unrar has no per-file callback either, so resolve collisions up front
        # and let "rename" fall out of unrar's own -or (auto-rename) mode.
        skip: set[str] = set()
        any_rename = False
        for entry in files:
            rel = entry.name if flatten else entry.path
            target = os.path.join(destination, rel)
            if os.path.lexists(target):
                resolution = on_conflict(target, entry) if on_conflict else Resolution.RENAME
                if resolution is Resolution.CANCEL:
                    raise Cancelled()
                if resolution is Resolution.SKIP:
                    skip.add(entry.path)
                elif resolution is Resolution.RENAME:
                    any_rename = True

        wanted = [e for e in files if e.path not in skip]
        if not wanted:
            return []
        for entry in wanted:
            self.safe_join(destination, entry.name if flatten else entry.path)

        # -o+ overwrite, -or rename automatically, -x exclude
        overwrite = "-or" if any_rename else "-o+"
        mode = "e" if flatten else "x"
        argv = [self._exe(), mode, "-idc", overwrite, *self._password_args(), self.path]
        if skip or entries is not None:
            argv += [e.path for e in wanted]
        argv.append(destination + os.sep)

        self._stream(argv, progress or Progress())

        written = []
        for entry in wanted:
            rel = entry.name if flatten else entry.path
            candidate = os.path.join(destination, rel)
            if os.path.lexists(candidate):
                written.append(candidate)
        if progress:
            progress.current = progress.total
            progress.set_message("Finished")
        return written

    def _stream(self, argv: list[str], progress: Progress) -> None:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, bufsize=0)
        progress.on_cancel(lambda: _kill(proc))
        assert proc.stdout is not None
        try:
            while True:
                chunk = proc.stdout.read(128)
                if not chunk:
                    break
                found = _PERCENT_RE.findall(chunk)
                if found:
                    percent = int(found[-1])
                    if 0 <= percent <= 100:
                        progress.current = percent * 10
                        progress._emit()
                if progress.cancelled:
                    _kill(proc)
                    raise Cancelled()
        finally:
            stderr = proc.stderr.read() if proc.stderr else b""
            proc.wait()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        if progress.cancelled:
            raise Cancelled()
        if proc.returncode != 0:
            raise self._translate(proc.returncode, stderr.decode("utf-8", "replace"))

    def test(self, progress: Progress | None = None) -> list[str]:
        if progress:
            progress.begin(1000, "Checking…")
        try:
            self._stream([self._exe(), "t", "-idc", *self._password_args(), self.path],
                         progress or Progress())
        except ArchiveError as exc:
            return [exc.detail or exc.message]
        return []


def _int(value: str | None) -> int:
    try:
        return int((value or "0").split()[0])
    except (ValueError, IndexError):
        return 0


def _timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(value.split(",")[0].split(".")[0].strip(), fmt)
        except ValueError:
            continue
    return None
