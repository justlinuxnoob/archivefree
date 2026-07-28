"""Universal backend driving the 7-Zip command-line tool.

This covers everything the pure-Python backends can't: 7z, RAR, ISO, CAB, DMG,
MSI, WIM, and AES-encrypted ZIPs. It's the fallback, not the default — shelling
out costs a process spawn per operation, so ZIP and TAR use the stdlib instead.

Two details are worth knowing:

* We always pass ``-p`` (with an empty password when we have none). Without it,
  7-Zip prompts on the terminal and the app hangs forever with no visible
  cause. With it, an encrypted archive fails immediately and we can prompt
  properly in the UI.
* Conflicts are resolved *before* extraction starts rather than mid-run,
  because 7-Zip gives no hook to intervene per-file. Entries the user wants
  renamed are unpacked into a staging directory on the same filesystem and then
  moved into place, so the rename costs a directory update, not a copy.
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable

from .. import detect, tools
from ..conflict import Resolution, unique_path
from ..entry import ArchiveEntry, ArchiveInfo, normalise_path
from ..errors import (
    ArchiveError,
    CorruptArchive,
    MissingVolume,
    PasswordRequired,
    UnsupportedFormat,
    WrongPassword,
)
from ..jobs import Cancelled, Progress
from .base import Backend

_PROGRESS_RE = re.compile(rb"(\d{1,3})%")
_ATTR_DIR = "D"


class SevenZipBackend(Backend):
    formats = (
        "7z", "rar", "iso", "cab", "deb", "rpm", "ar", "cpio", "lha", "arj",
        "wim", "dmg", "xar", "chm", "msi", "vhd", "squashfs", "zip", "tar",
    )
    priority = 10  # last resort; stdlib backends outrank it

    def __init__(self, path: str, fmt: str, password: str | None = None):
        super().__init__(path, fmt, password)
        self._entries: list[ArchiveEntry] | None = None
        self._info: ArchiveInfo | None = None
        self._volumes: list[str] | None = None
        #: Set when 7-Zip had to be told the container type explicitly; every
        #: later command must pass the same one or it recurses into a nested
        #: archive and operates on the wrong layer.
        self._type_override: str | None = None

    @classmethod
    def is_available(cls) -> bool:
        return tools.sevenzip() is not None

    @property
    def supports_preview(self) -> bool:
        return True

    # -- process plumbing ------------------------------------------------
    def _exe(self) -> str:
        return tools.require("7z", f"open {detect.FORMATS[self.format].label} files")

    def _type_args(self) -> list[str]:
        return [f"-t{self._type_override}"] if self._type_override else []

    def _password_args(self) -> list[str]:
        # An empty -p turns an interactive prompt into a clean error.
        return [f"-p{self.password}" if self.password else "-p"]

    def _run(self, args: list[str], progress: Progress | None = None,
             capture_stdout: bool = True) -> subprocess.CompletedProcess:
        # Note: no -bse1 here. That flag folds errors into stdout, which would
        # leave stderr empty and make every failure look like a generic one.
        argv = [self._exe(), *args, *self._type_args(), "-y", *self._password_args()]
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        if progress:
            progress.on_cancel(lambda: _kill(proc))
        try:
            out, err = proc.communicate()
        except Exception:
            _kill(proc)
            raise
        if progress and progress.cancelled:
            raise Cancelled()
        return subprocess.CompletedProcess(argv, proc.returncode, out or b"", err or b"")

    def _run_streaming(self, args: list[str], progress: Progress, weight: int) -> None:
        """Run 7-Zip with percentage progress piped back to the UI."""
        argv = [self._exe(), *args, *self._type_args(), "-y", "-bsp1",
                *self._password_args()]
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, bufsize=0,
        )
        progress.on_cancel(lambda: _kill(proc))

        buffer = b""
        # Keep a rolling tail of stdout: some failures ("Can't open as archive")
        # are reported there rather than on stderr, and we need it to explain why.
        tail = b""
        assert proc.stdout is not None
        try:
            while True:
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                buffer += chunk
                # 7-Zip redraws the percentage using \r and \b, not newlines.
                matches = _PROGRESS_RE.findall(buffer[-256:])
                if matches:
                    percent = int(matches[-1])
                    if 0 <= percent <= 100:
                        progress.current = int(weight * percent / 100)
                        progress._emit()
                if len(buffer) > 4096:
                    tail = buffer[-2048:]
                    buffer = buffer[-512:]
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
            raise self._translate(proc.returncode, _diagnostics(tail + buffer, stderr))

    # -- error translation -----------------------------------------------
    def _translate(self, code: int, stderr: str) -> ArchiveError:
        low = stderr.lower()
        if "wrong password" in low or "cannot open encrypted" in low:
            return WrongPassword() if self.password else PasswordRequired()
        # 7-Zip says only "Can't open as archive" when a header-encrypted file
        # is opened with the wrong key, so the password we hold disambiguates it.
        if ("can not open the file as archive" in low
                or "can't open as archive" in low) and self.password:
            return WrongPassword()
        if "is not supported archive" in low or "cannot open the file as" in low:
            return UnsupportedFormat(
                "ArchiveFree doesn’t recognise this file as an archive it can open.",
                detail=stderr.strip(),
            )
        if "missing volume" in low or "cannot find volume" in low:
            match = re.search(r"volume\s*:?\s*(\S+)", stderr, re.I)
            return MissingVolume(match.group(1) if match else "a later part",
                                 detail=stderr.strip())
        if "there is no such archive" in low or "cannot find archive" in low:
            return ArchiveError("That file no longer exists.", detail=stderr.strip())
        if "not enough space" in low or "no space left" in low:
            from ..errors import DiskFull

            return DiskFull(os.path.dirname(self.path))
        if "data error" in low or "crc failed" in low or "unexpected end of" in low:
            return CorruptArchive(
                "This archive is damaged — some of its contents can’t be recovered.",
                detail=stderr.strip(),
            )
        if "can not open output file" in low or "access is denied" in low:
            return ArchiveError(
                "ArchiveFree doesn’t have permission to write there.",
                detail=stderr.strip(),
                hint="Choose a different destination folder.",
            )
        return ArchiveError(
            "The archive tool reported a problem.",
            detail=stderr.strip() or f"7z exited with status {code}",
        )

    # -- reading ---------------------------------------------------------
    def info(self) -> ArchiveInfo:
        if self._info is None:
            self.list_entries()
        assert self._info is not None
        return self._info

    def list_entries(self, progress: Progress | None = None) -> list[ArchiveEntry]:
        if self._entries is not None:
            return self._entries
        if progress:
            progress.begin(0, "Reading archive…")

        result = self._run(["l", "-slt", self.path], progress=progress)
        if result.returncode != 0:
            raise self._translate(result.returncode, _diagnostics(result.stdout, result.stderr))

        text = result.stdout.decode("utf-8", "replace")
        entries, encrypted_any = _parse_slt(text)

        # 7-Zip descends into nested containers on its own: listing a .deb makes
        # it open the data.tar.xz inside and report *that* archive's contents,
        # whose entries carry no Path at all. The result is an empty listing for
        # a perfectly good package. Pinning the outer type stops the recursion.
        if not entries:
            outer = _outer_type(text)
            if outer:
                retry = self._run(["l", "-slt", f"-t{outer}", self.path],
                                  progress=progress)
                if retry.returncode == 0:
                    parsed, encrypted_any = _parse_slt(
                        retry.stdout.decode("utf-8", "replace"))
                    if parsed:
                        entries = parsed
                        # Remember it: extraction has to pin the same type or
                        # 7-Zip descends again and unpacks the wrong layer.
                        self._type_override = outer
        volumes = detect.split_volumes(self.path)

        self._entries = entries
        self._info = ArchiveInfo(
            path=self.path,
            format=self.format,
            format_label=detect.FORMATS[self.format].label,
            entry_count=sum(1 for e in entries if not e.is_dir),
            total_size=sum(e.size for e in entries if not e.is_dir),
            archive_size=sum(os.path.getsize(v) for v in volumes) if volumes
            else os.path.getsize(self.path),
            encrypted=encrypted_any,
            volumes=volumes,
        )
        return entries

    def read_member(self, entry: ArchiveEntry, limit: int = 4 * 1024 * 1024) -> bytes:
        argv = [self._exe(), "x", "-so", self.path, entry.path,
                *self._type_args(), "-y", "-bso0", "-bse1", *self._password_args()]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
        try:
            assert proc.stdout is not None
            data = proc.stdout.read(limit)
            _kill(proc)  # stop decompressing the rest; we only wanted a preview
            return data
        finally:
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
        selected = list(entries) if entries is not None else all_entries
        selected = _expand_folders(selected, all_entries)
        files = [e for e in selected if not e.is_dir]
        if not files:
            return []

        os.makedirs(destination, exist_ok=True)
        if progress:
            progress.begin(1000, "Preparing…")

        # Decide what to do about every collision before unpacking anything.
        skip: set[str] = set()
        rename: set[str] = set()
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
                    rename.add(entry.path)

        direct = [e for e in files if e.path not in skip and e.path not in rename]
        staged = [e for e in files if e.path in rename]
        written: list[str] = []

        # Guard against a malicious archive before 7-Zip touches the disk.
        for entry in direct + staged:
            self.safe_join(destination, entry.name if flatten else entry.path)

        mode = "e" if flatten else "x"
        select_all = entries is None and not skip and not rename and not flatten

        if direct:
            if progress:
                progress.set_message("Extracting…")
            args = [mode, self.path, f"-o{destination}", "-aoa"]
            if not select_all:
                args += [e.path for e in direct]
            self._run_streaming(args, progress or Progress(), weight=900 if staged else 1000)
            written += [
                os.path.join(destination, e.name if flatten else e.path) for e in direct
            ]

        if staged:
            if progress:
                progress.set_message("Extracting renamed items…")
            with tempfile.TemporaryDirectory(dir=destination, prefix=".archivefree-") as tmp:
                args = [mode, self.path, f"-o{tmp}", "-aoa"] + [e.path for e in staged]
                self._run_streaming(args, progress or Progress(), weight=1000)
                for entry in staged:
                    rel = entry.name if flatten else entry.path
                    source = os.path.join(tmp, rel)
                    if not os.path.lexists(source):
                        continue
                    target = unique_path(os.path.join(destination, rel))
                    os.makedirs(os.path.dirname(target) or destination, exist_ok=True)
                    shutil.move(source, target)
                    written.append(target)

        if progress:
            progress.current = progress.total
            progress.set_message("Finished")
        return written

    def test(self, progress: Progress | None = None) -> list[str]:
        if progress:
            progress.begin(1000, "Checking…")
        try:
            self._run_streaming(["t", self.path], progress or Progress(), weight=1000)
        except CorruptArchive as exc:
            return [exc.detail or exc.message]
        except ArchiveError as exc:
            return [exc.message]
        return []


# -- parsing -------------------------------------------------------------


def _parse_slt(text: str) -> tuple[list[ArchiveEntry], bool]:
    """Parse ``7z l -slt`` output into entries.

    The listing is "Key = Value" blocks separated by blank lines, preceded by a
    header we skip by waiting for the ``----------`` divider.
    """
    entries: list[ArchiveEntry] = []
    encrypted_any = False

    body = text.split("\n----------\n", 1)
    if len(body) < 2:
        return entries, False

    for block in body[1].split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition(" = ")
            if sep:
                fields[key.strip()] = value.strip()
        raw_path = fields.get("Path")
        if not raw_path:
            continue

        attributes = fields.get("Attributes", "")
        folder = fields.get("Folder", "")
        is_dir = folder == "+" or attributes.startswith(_ATTR_DIR)
        encrypted = fields.get("Encrypted", "") == "+"
        encrypted_any = encrypted_any or encrypted

        path = normalise_path(raw_path)
        if not path:
            continue

        entries.append(
            ArchiveEntry(
                path=path,
                size=_int(fields.get("Size")),
                compressed_size=_int(fields.get("Packed Size")),
                modified=_timestamp(fields.get("Modified")),
                is_dir=is_dir,
                is_symlink="l" in attributes.split()[-1] if " " in attributes else False,
                encrypted=encrypted,
                crc=fields.get("CRC") or None,
                token=raw_path,
            )
        )
    return entries, encrypted_any


def _outer_type(text: str) -> str | None:
    """The 7-Zip type name of the outermost archive, e.g. "Ar" for a .deb.

    Taken from the header block, before any nested archive is described.
    """
    header = text.split("\n----------\n", 1)[0]
    for line in header.splitlines():
        key, sep, value = line.partition(" = ")
        if sep and key.strip() == "Type":
            name = value.strip().lower()
            return name or None
    return None


def _int(value: str | None) -> int:
    try:
        return int(value) if value else 0
    except ValueError:
        return 0


def _timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(value.split(".")[0], fmt)
        except ValueError:
            continue
    return None


def _expand_folders(selected: list[ArchiveEntry], all_entries: list[ArchiveEntry]
                    ) -> list[ArchiveEntry]:
    """Selecting a folder selects everything inside it."""
    chosen = {e.path for e in selected}
    extra: list[ArchiveEntry] = []
    prefixes = [e.path + "/" for e in selected if e.is_dir]
    if prefixes:
        for entry in all_entries:
            if entry.path in chosen:
                continue
            if any(entry.path.startswith(p) for p in prefixes):
                extra.append(entry)
    return selected + extra


def _diagnostics(stdout: bytes, stderr: bytes) -> str:
    """Combine both streams, keeping only the lines that explain a failure.

    7-Zip scatters its diagnostics across stdout and stderr depending on the
    subcommand, so we look at both and drop the banner and progress noise.
    """
    text = (stderr.decode("utf-8", "replace") + "\n"
            + stdout.decode("utf-8", "replace"))
    interesting = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
        and not line.startswith(("7-Zip", " 64-bit", "Scanning", "Listing", "Open ",
                                 "--", "Path =", "Type =", "Physical Size"))
    ]
    return "\n".join(dict.fromkeys(interesting))[:2000]


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass
