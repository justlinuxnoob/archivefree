"""Creating archives.

Deliberately narrow: ZIP, 7z and the tar family cover essentially everything a
Linux user needs to *produce*, and each maps to a compression level, optional
password and optional splitting. Reading supports far more formats than writing
does, which is the right trade — you receive exotic formats, you don't make them.

ZIP and TAR are written with the stdlib. 7z, encrypted ZIP and split volumes go
through 7-Zip, because implementing AES and volume splitting by hand would be
both slower and a worse idea.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass

from . import tools
from .backends.sevenzip import _kill
from .errors import ArchiveError, DiskFull
from .jobs import Cancelled, Progress

#: Compression levels offered in the UI: (key, label, description).
LEVELS = [
    ("store", "None", "Fastest — just bundles the files together"),
    ("fast", "Fast", "Light compression, quick to finish"),
    ("normal", "Normal", "A good balance — recommended"),
    ("maximum", "Maximum", "Smallest file, takes noticeably longer"),
]

#: Per-format mapping from our level names to each tool's own scale.
_ZIP_LEVELS = {"store": 0, "fast": 3, "normal": 6, "maximum": 9}
_GZ_LEVELS = {"store": 1, "fast": 3, "normal": 6, "maximum": 9}
_XZ_LEVELS = {"store": 0, "fast": 2, "normal": 6, "maximum": 9}
_ZSTD_LEVELS = {"store": 1, "fast": 3, "normal": 10, "maximum": 19}
_SEVENZIP_LEVELS = {"store": 0, "fast": 3, "normal": 5, "maximum": 9}


@dataclass
class CreateOptions:
    """Everything the create-archive dialog collects."""

    destination: str
    format: str = "zip"
    level: str = "normal"
    password: str | None = None
    #: Encrypt the file *names* too, not just contents. 7z and RAR only.
    encrypt_names: bool = False
    #: Split into volumes of this many bytes. 0 disables splitting.
    split_bytes: int = 0
    #: Store paths relative to this directory.
    base_dir: str | None = None
    follow_symlinks: bool = False

    @property
    def needs_sevenzip(self) -> bool:
        return (
            self.format == "7z"
            or bool(self.password)
            or self.split_bytes > 0
        )


#: One line per creatable format, explaining the trade-off in plain terms.
CREATABLE_HINT = {
    "zip": "Readable on Windows, macOS and Linux without extra software. "
           "The safest choice when sending files to someone else.",
    "7z": "Compresses noticeably smaller than ZIP, and supports strong "
          "encryption. The recipient may need to install a 7-Zip tool.",
    "tar.gz": "The usual choice on Linux. Fast, and preserves file permissions.",
    "tar.xz": "Smaller than tar.gz, but takes longer to create and to open.",
    "tar.zst": "Nearly as small as tar.xz and much faster. Needs a recent system.",
    "tar.bz2": "Older than tar.xz and slower, but understood almost everywhere.",
    "tar": "Bundles files together without compressing them. Instant, but no "
           "size saving.",
}

SPLIT_PRESETS = [
    ("None", 0),
    ("10 MB", 10 * 1000 * 1000),
    ("100 MB", 100 * 1000 * 1000),
    ("700 MB (CD)", 700 * 1000 * 1000),
    ("1 GB", 1000 * 1000 * 1000),
    ("4.4 GB (DVD)", 4400 * 1000 * 1000),
]


def default_archive_name(sources: list[str], fmt: str) -> str:
    """Name the archive after the single item, or the parent folder, being packed."""
    from . import detect

    ext = detect.FORMATS[fmt].extensions[0]
    if len(sources) == 1:
        base = os.path.basename(os.path.normpath(sources[0]))
        stem = base
        if os.path.isfile(sources[0]) and "." in base:
            stem = base.rsplit(".", 1)[0]
        return f"{stem}{ext}"
    parent = os.path.basename(os.path.dirname(os.path.normpath(sources[0])))
    return f"{parent or 'archive'}{ext}"


def _collect(sources: list[str], base_dir: str | None,
             follow_symlinks: bool, progress: Progress | None) -> list[tuple[str, str]]:
    """Walk the sources into (absolute path, path stored in the archive) pairs."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[int, int]] = set()

    for source in sources:
        source = os.path.abspath(source)
        root = base_dir or os.path.dirname(source)
        if os.path.isfile(source) or os.path.islink(source):
            pairs.append((source, os.path.relpath(source, root)))
            continue
        for dirpath, dirnames, filenames in os.walk(source, followlinks=follow_symlinks):
            if progress:
                progress.check()
            dirnames.sort()
            filenames.sort()
            # Don't recurse into a directory we've already visited via a symlink.
            if follow_symlinks:
                try:
                    stat = os.stat(dirpath)
                    key = (stat.st_dev, stat.st_ino)
                    if key in seen:
                        dirnames[:] = []
                        continue
                    seen.add(key)
                except OSError:
                    continue
            if not dirnames and not filenames:
                pairs.append((dirpath, os.path.relpath(dirpath, root)))
            for name in filenames:
                full = os.path.join(dirpath, name)
                pairs.append((full, os.path.relpath(full, root)))
    return pairs


def create_archive(sources: list[str], options: CreateOptions,
                   progress: Progress | None = None) -> str:
    """Create an archive from ``sources``. Returns the path written."""
    if not sources:
        raise ArchiveError("Nothing was selected to compress.")

    destination = options.destination
    os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)

    if options.needs_sevenzip:
        return _create_with_sevenzip(sources, options, progress)
    if options.format == "zip":
        return _create_zip(sources, options, progress)
    return _create_tar(sources, options, progress)


# -- ZIP -----------------------------------------------------------------


def _create_zip(sources: list[str], options: CreateOptions,
                progress: Progress | None) -> str:
    pairs = _collect(sources, options.base_dir, options.follow_symlinks, progress)
    total = sum(_size_of(p) for p, _ in pairs) or 1
    if progress:
        progress.begin(total, "Compressing…")

    level = _ZIP_LEVELS[options.level]
    method = zipfile.ZIP_STORED if options.level == "store" else zipfile.ZIP_DEFLATED
    done = 0
    temp = options.destination + ".part"

    try:
        with zipfile.ZipFile(
            temp, "w", compression=method,
            compresslevel=level if method != zipfile.ZIP_STORED else None,
            allowZip64=True,
        ) as zf:
            for full, arcname in pairs:
                if progress:
                    progress.check()
                    progress.set_message(os.path.basename(full))
                if os.path.islink(full) and not options.follow_symlinks:
                    _zip_symlink(zf, full, arcname)
                    continue
                if os.path.isdir(full):
                    zf.write(full, arcname + "/")
                    continue
                zf.write(full, arcname)
                done += _size_of(full)
                if progress:
                    progress.current = min(done, total)
                    progress._emit()
    except Cancelled:
        _cleanup(temp)
        raise
    except OSError as exc:
        _cleanup(temp)
        raise _write_error(exc, options.destination) from exc

    os.replace(temp, options.destination)
    if progress:
        progress.current = progress.total
        progress.set_message("Finished")
    return options.destination


def _zip_symlink(zf: zipfile.ZipFile, full: str, arcname: str) -> None:
    """Store a symlink as a link, the way Info-ZIP does, not as its target."""
    target = os.readlink(full)
    info = zipfile.ZipInfo(arcname)
    info.create_system = 3  # Unix
    info.external_attr = (0o120777 << 16)  # S_IFLNK | 0777
    zf.writestr(info, target)


# -- TAR -----------------------------------------------------------------


def _create_tar(sources: list[str], options: CreateOptions,
                progress: Progress | None) -> str:
    fmt = options.format
    pairs = _collect(sources, options.base_dir, options.follow_symlinks, progress)
    total = sum(_size_of(p) for p, _ in pairs) or 1
    if progress:
        progress.begin(total, "Compressing…")

    temp = options.destination + ".part"
    done = 0
    external = None

    if fmt == "tar":
        mode, kwargs = "w", {}
    elif fmt == "tar.gz":
        mode, kwargs = "w:gz", {"compresslevel": _GZ_LEVELS[options.level]}
    elif fmt == "tar.bz2":
        mode, kwargs = "w:bz2", {"compresslevel": max(1, _GZ_LEVELS[options.level])}
    elif fmt == "tar.xz":
        mode, kwargs = "w:xz", {"preset": _XZ_LEVELS[options.level]}
    elif fmt in ("tar.zst", "tar.lz4"):
        # No stdlib codec: write an uncompressed tar into the compressor's stdin.
        external = fmt
        mode, kwargs = "w|", {}
    else:
        raise ArchiveError(f"ArchiveFree can’t create {fmt} archives.")

    try:
        if external:
            _tar_through_pipe(pairs, options, external, temp, progress, total)
        else:
            with tarfile.open(temp, mode, **kwargs) as tf:
                for full, arcname in pairs:
                    if progress:
                        progress.check()
                        progress.set_message(os.path.basename(full))
                    tf.add(full, arcname, recursive=False,
                           filter=_strip_owner)
                    done += _size_of(full)
                    if progress:
                        progress.current = min(done, total)
                        progress._emit()
    except Cancelled:
        _cleanup(temp)
        raise
    except OSError as exc:
        _cleanup(temp)
        raise _write_error(exc, options.destination) from exc

    os.replace(temp, options.destination)
    if progress:
        progress.current = progress.total
        progress.set_message("Finished")
    return options.destination


def _tar_through_pipe(pairs, options: CreateOptions, fmt: str, temp: str,
                      progress: Progress | None, total: int) -> None:
    """Stream a tar into zstd/lz4's stdin, writing its stdout to ``temp``."""
    binary = "zstd" if fmt == "tar.zst" else "lz4"
    exe = tools.require(binary, f"create .{binary} archives")
    level = _ZSTD_LEVELS[options.level] if binary == "zstd" else \
        {"store": 1, "fast": 1, "normal": 6, "maximum": 12}[options.level]

    with open(temp, "wb") as out:
        proc = subprocess.Popen(
            [exe, f"-{level}", "-c", "-T0" if binary == "zstd" else "-"],
            stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE,
        )
        if progress:
            progress.on_cancel(lambda: _kill(proc))
        done = 0
        try:
            with tarfile.open(fileobj=proc.stdin, mode="w|") as tf:
                for full, arcname in pairs:
                    if progress:
                        progress.check()
                        progress.set_message(os.path.basename(full))
                    tf.add(full, arcname, recursive=False, filter=_strip_owner)
                    done += _size_of(full)
                    if progress:
                        progress.current = min(done, total)
                        progress._emit()
        finally:
            if proc.stdin:
                proc.stdin.close()
            stderr = proc.stderr.read() if proc.stderr else b""
            proc.wait()
            if proc.stderr:
                proc.stderr.close()
        if proc.returncode != 0:
            raise ArchiveError(f"The {binary} compressor failed.",
                               detail=stderr.decode("utf-8", "replace"))


def _strip_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Drop uid/gid and owner names.

    Archives shared between machines shouldn't carry the creator's account
    details, and root-owned entries cause confusing permission errors when
    someone else unpacks them.
    """
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


# -- 7-Zip (7z, encryption, splitting) -----------------------------------


def _create_with_sevenzip(sources: list[str], options: CreateOptions,
                          progress: Progress | None) -> str:
    exe = tools.require("7z", "create 7z, encrypted or split archives")
    fmt = options.format
    type_flag = {
        "7z": "-t7z", "zip": "-tzip", "tar": "-ttar",
        "tar.gz": "-tgzip", "tar.bz2": "-tbzip2", "tar.xz": "-txz",
    }.get(fmt)
    if type_flag is None:
        raise ArchiveError(f"ArchiveFree can’t create {fmt} archives with a password.")

    if fmt.startswith("tar.") and (options.password or options.split_bytes):
        raise ArchiveError(
            "TAR archives can’t be password-protected.",
            hint="Choose ZIP or 7z if you need a password.",
        )

    destination = options.destination
    if progress:
        progress.begin(1000, "Compressing…")

    args = [exe, "a", type_flag, f"-mx={_SEVENZIP_LEVELS[options.level]}",
            "-y", "-bsp1", "-bse1"]
    if options.password:
        args.append(f"-p{options.password}")
        if fmt == "7z" and options.encrypt_names:
            args.append("-mhe=on")
        elif fmt == "zip":
            args.append("-mem=AES256")
    if options.split_bytes:
        args.append(f"-v{options.split_bytes}b")

    # Store paths relative to base_dir so the archive doesn't embed /home/you/...
    base = options.base_dir or os.path.dirname(os.path.abspath(sources[0]))
    rel_sources = [os.path.relpath(os.path.abspath(s), base) for s in sources]
    args += [destination, "--", *rel_sources]

    _existing = _remove_stale_volumes(destination)
    proc = subprocess.Popen(args, cwd=base, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0)
    if progress:
        progress.on_cancel(lambda: _kill(proc))

    from .backends.sevenzip import _PROGRESS_RE

    assert proc.stdout is not None
    try:
        while True:
            chunk = proc.stdout.read(128)
            if not chunk:
                break
            found = _PROGRESS_RE.findall(chunk)
            if found and progress:
                percent = int(found[-1])
                if 0 <= percent <= 100:
                    progress.current = percent * 10
                    progress._emit()
            if progress and progress.cancelled:
                _kill(proc)
                raise Cancelled()
    finally:
        stderr = proc.stderr.read() if proc.stderr else b""
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    if progress and progress.cancelled:
        _cleanup(destination)
        raise Cancelled()
    if proc.returncode != 0:
        _cleanup(destination)
        message = stderr.decode("utf-8", "replace")
        if "no space left" in message.lower():
            raise DiskFull(os.path.dirname(destination))
        raise ArchiveError("Creating the archive failed.", detail=message.strip())

    if progress:
        progress.current = progress.total
        progress.set_message("Finished")
    # A split archive's first part is what we hand back to the caller.
    return destination + ".001" if options.split_bytes else destination


def _remove_stale_volumes(destination: str) -> None:
    """7-Zip appends to an existing archive, so clear old volumes first."""
    directory = os.path.dirname(os.path.abspath(destination))
    base = os.path.basename(destination)
    try:
        for name in os.listdir(directory):
            if name == base or (name.startswith(base + ".") and name[len(base) + 1:].isdigit()):
                _cleanup(os.path.join(directory, name))
    except OSError:
        pass


def _size_of(path: str) -> int:
    try:
        return os.lstat(path).st_size if not os.path.isdir(path) else 0
    except OSError:
        return 0


def _cleanup(path: str) -> None:
    try:
        if os.path.lexists(path):
            os.unlink(path)
    except OSError:
        pass


def _write_error(exc: OSError, destination: str) -> ArchiveError:
    import errno

    if exc.errno == errno.ENOSPC:
        return DiskFull(os.path.dirname(destination))
    if exc.errno in (errno.EACCES, errno.EPERM):
        return ArchiveError(
            "ArchiveFree doesn’t have permission to save there.",
            detail=str(exc),
            hint="Try saving to your Home or Documents folder instead.",
        )
    return ArchiveError("Creating the archive failed.", detail=str(exc))
