"""Archive format detection.

Content sniffing comes first (file extensions lie, and people rename things),
with the extension used only as a tie-breaker or when the magic bytes are
inconclusive. Everything here is cheap: we read at most 512 bytes.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Format:
    key: str
    label: str
    mime: str
    # Extensions, longest-first, used for naming and for extension fallback.
    extensions: tuple[str, ...]
    # True when the container holds exactly one stream with no filename table
    # (plain .gz/.bz2/.xz/.zst) — these get a synthesised single entry.
    single_stream: bool = False
    can_create: bool = True
    supports_password: bool = False
    supports_split: bool = False


FORMATS: dict[str, Format] = {
    f.key: f
    for f in [
        Format("zip", "ZIP archive", "application/zip", (".zip",),
               supports_password=True, supports_split=True),
        Format("7z", "7-Zip archive", "application/x-7z-compressed", (".7z",),
               supports_password=True, supports_split=True),
        Format("rar", "RAR archive", "application/vnd.rar", (".rar",),
               can_create=False, supports_password=True, supports_split=True),
        Format("tar", "TAR archive", "application/x-tar", (".tar",)),
        Format("tar.gz", "Gzip-compressed TAR", "application/gzip",
               (".tar.gz", ".tgz", ".taz")),
        Format("tar.bz2", "Bzip2-compressed TAR", "application/x-bzip2",
               (".tar.bz2", ".tbz", ".tbz2", ".tb2")),
        Format("tar.xz", "XZ-compressed TAR", "application/x-xz",
               (".tar.xz", ".txz")),
        Format("tar.zst", "Zstandard-compressed TAR", "application/zstd",
               (".tar.zst", ".tzst")),
        Format("tar.lz4", "LZ4-compressed TAR", "application/x-lz4", (".tar.lz4",)),
        Format("tar.lzma", "LZMA-compressed TAR", "application/x-lzma", (".tar.lzma",)),
        Format("gz", "Gzip file", "application/gzip", (".gz",), single_stream=True),
        Format("bz2", "Bzip2 file", "application/x-bzip2", (".bz2",), single_stream=True),
        Format("xz", "XZ file", "application/x-xz", (".xz",), single_stream=True),
        Format("zst", "Zstandard file", "application/zstd", (".zst",), single_stream=True),
        Format("lz4", "LZ4 file", "application/x-lz4", (".lz4",), single_stream=True),
        Format("lzma", "LZMA file", "application/x-lzma", (".lzma",), single_stream=True),
        Format("iso", "ISO disc image", "application/x-cd-image", (".iso",),
               can_create=False),
        Format("cab", "Windows cabinet", "application/vnd.ms-cab-compressed",
               (".cab",), can_create=False),
        Format("deb", "Debian package", "application/vnd.debian.binary-package",
               (".deb",), can_create=False),
        Format("rpm", "RPM package", "application/x-rpm", (".rpm",), can_create=False),
        Format("ar", "AR archive", "application/x-archive", (".a", ".ar"), can_create=False),
        Format("cpio", "CPIO archive", "application/x-cpio", (".cpio",), can_create=False),
        Format("lha", "LHA archive", "application/x-lha", (".lha", ".lzh"), can_create=False),
        Format("arj", "ARJ archive", "application/x-arj", (".arj",), can_create=False),
        Format("wim", "Windows image", "application/x-ms-wim", (".wim", ".swm"),
               can_create=False),
        Format("dmg", "Apple disk image", "application/x-apple-diskimage", (".dmg",),
               can_create=False),
        Format("xar", "XAR archive", "application/x-xar", (".xar", ".pkg"), can_create=False),
        Format("chm", "Compiled help file", "application/vnd.ms-htmlhelp", (".chm",),
               can_create=False),
        Format("msi", "Windows installer", "application/x-msi", (".msi",), can_create=False),
        Format("vhd", "Virtual hard disk", "application/x-vhd", (".vhd", ".vhdx"),
               can_create=False),
        Format("squashfs", "SquashFS image", "application/vnd.squashfs",
               (".squashfs", ".sqsh"), can_create=False),
        Format("z", "Compress file", "application/x-compress", (".z",),
               single_stream=True, can_create=False),
    ]
}

#: Formats offered in the "Create archive" dialog, in the order shown.
CREATABLE = ["zip", "7z", "tar.gz", "tar.xz", "tar.zst", "tar.bz2", "tar"]

#: Extension list sorted longest-first so ".tar.gz" wins over ".gz".
_EXT_INDEX: list[tuple[str, str]] = sorted(
    ((ext, f.key) for f in FORMATS.values() for ext in f.extensions),
    key=lambda pair: -len(pair[0]),
)

# Split-volume naming schemes we recognise, mapping a filename to its part
# number.  .7z.001 / .zip.001 / .part1.rar / .r00 / .z01
_SPLIT_PATTERNS = [
    re.compile(r"^(?P<stem>.+)\.(?P<num>\d{3,4})$"),            # foo.7z.001
    re.compile(r"^(?P<stem>.+)\.part(?P<num>\d+)\.rar$", re.I),  # foo.part1.rar
    re.compile(r"^(?P<stem>.+)\.r(?P<num>\d{2})$", re.I),        # foo.r00
    re.compile(r"^(?P<stem>.+)\.z(?P<num>\d{2})$", re.I),        # foo.z01
]


def _sniff(head: bytes, path: str) -> str | None:
    """Identify a format from its leading bytes. Returns a format key or None."""
    if len(head) < 4:
        return None

    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:7] == b"Rar!\x1a\x07\x00" or head[:8] == b"Rar!\x1a\x07\x01\x00":
        return "rar"
    if head[:2] == b"\x1f\x8b":
        return "tar.gz" if _looks_like_tar_ext(path, ".gz") else "gz"
    if head[:3] == b"BZh":
        return "tar.bz2" if _looks_like_tar_ext(path, ".bz2") else "bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "tar.xz" if _looks_like_tar_ext(path, ".xz") else "xz"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "tar.zst" if _looks_like_tar_ext(path, ".zst") else "zst"
    if head[:4] == b"\x04\x22\x4d\x18":
        return "tar.lz4" if _looks_like_tar_ext(path, ".lz4") else "lz4"
    if head[:2] == b"\x1f\x9d":
        return "z"
    if head[:4] == b"MSCF":
        return "cab"
    if head[:4] == b"\xed\xab\xee\xdb":
        return "rpm"
    if head[:8] == b"!<arch>\n":
        # .deb is an ar archive whose first member is "debian-binary".
        return "deb" if b"debian-binary" in head[:80] else "ar"
    if head[:8] == b"MSWIM\x00\x00\x00":
        return "wim"
    if head[:4] in (b"hsqs", b"sqsh", b"qshs", b"hsqt"):
        return "squashfs"
    if head[:4] == b"xar!":
        return "xar"
    if head[:4] == b"ITSF":
        return "chm"
    if head[:2] == b"\x60\xea":
        return "arj"
    if head[2:6] in (b"-lh0", b"-lh1", b"-lh5", b"-lh6", b"-lh7", b"-lzs"):
        return "lha"
    if head[:6] == b"070701" or head[:6] == b"070707" or head[:2] == b"\xc7\x71":
        return "cpio"
    # LZMA alone has no real magic: 0x5d + dictionary size + 8-byte size field.
    if head[0:1] == b"\x5d" and head[1:5] in (b"\x00\x00\x80\x00", b"\x00\x00\x01\x00",
                                              b"\x00\x00\x10\x00", b"\x00\x10\x00\x00"):
        return "tar.lzma" if _looks_like_tar_ext(path, ".lzma") else "lzma"
    return None


def _looks_like_tar_ext(path: str, compressed_ext: str) -> bool:
    """Decide if a compressed stream wraps a tar, based on the filename.

    We can't know for certain without decompressing, and decompressing a 4 GB
    .gz just to check is exactly the sluggishness this app exists to avoid. The
    filename is right in practice, and the tar backend falls back to treating
    the stream as a single file if the tar header turns out to be bogus.
    """
    low = path.lower()
    if not low.endswith(compressed_ext):
        return False
    stem = low[: -len(compressed_ext)]
    if stem.endswith(".tar"):
        return True
    # .tgz / .tbz2 / .txz / .tzst style short forms
    return any(low.endswith(e) for e in (".tgz", ".taz", ".tbz", ".tbz2", ".tb2",
                                         ".txz", ".tzst"))


def _iso_check(fh) -> bool:
    """ISO 9660 puts "CD001" at offset 0x8001; UDF images vary."""
    try:
        fh.seek(0x8001)
        if fh.read(5) == b"CD001":
            return True
        fh.seek(0x9001)
        return fh.read(5) == b"CD001"
    except OSError:
        return False
    finally:
        fh.seek(0)


def format_from_extension(path: str) -> str | None:
    low = os.path.basename(path).lower()
    for ext, key in _EXT_INDEX:
        if low.endswith(ext):
            return key
    return None


def detect_format(path: str) -> str | None:
    """Return the format key for ``path``, or None if it isn't a known archive."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
            found = _sniff(head, path)
            if found:
                return found
            # ISO needs a seek past the system area, so it's checked separately.
            if os.path.getsize(path) > 0x8006 and _iso_check(fh):
                return "iso"
    except OSError:
        return None

    # A split volume's later parts have no magic of their own; and some formats
    # (tar, notably) have their magic at offset 257 rather than 0.
    if _is_tar(path):
        return "tar"
    return format_from_extension(path)


def _is_tar(path: str) -> bool:
    """POSIX tar stores "ustar" at offset 257."""
    try:
        with open(path, "rb") as fh:
            fh.seek(257)
            return fh.read(5) in (b"ustar",)
    except OSError:
        return False


def describe(key: str | None) -> Format | None:
    return FORMATS.get(key) if key else None


def split_volumes(path: str) -> list[str]:
    """Find every part of a split archive, in order.

    Returns an empty list when ``path`` is not part of a split set. Only parts
    that actually exist on disk are returned; the caller reports gaps.
    """
    directory = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)

    for pattern in _SPLIT_PATTERNS:
        m = pattern.match(name)
        if not m:
            continue
        stem = m.group("stem")
        width = len(m.group("num"))
        siblings: list[tuple[int, str]] = []
        try:
            listing = os.listdir(directory)
        except OSError:
            return []
        for other in listing:
            om = pattern.match(other)
            if om and om.group("stem") == stem and len(om.group("num")) == width:
                siblings.append((int(om.group("num")), os.path.join(directory, other)))
        if len(siblings) > 1:
            siblings.sort()
            return [p for _, p in siblings]
        # A ".rar" first part pairs with ".r00", ".r01"...
    if name.lower().endswith(".rar"):
        stem = name[:-4]
        extra = []
        try:
            for other in os.listdir(directory):
                m = re.match(rf"^{re.escape(stem)}\.r(\d{{2}})$", other, re.I)
                if m:
                    extra.append((int(m.group(1)), os.path.join(directory, other)))
        except OSError:
            return []
        if extra:
            extra.sort()
            return [os.path.join(directory, name)] + [p for _, p in extra]
    return []


def first_volume(path: str) -> str:
    """Given any part of a split archive, return the part to actually open."""
    parts = split_volumes(path)
    return parts[0] if parts else path


def missing_volumes(parts: list[str]) -> list[str]:
    """Report gaps in a numbered volume sequence (e.g. .001, .002, .004)."""
    if len(parts) < 2:
        return []
    nums = []
    for p in parts:
        m = re.search(r"(\d+)$", os.path.basename(p))
        if not m:
            return []
        nums.append(int(m.group(1)))
    lo, hi = min(nums), max(nums)
    have = set(nums)
    return [f"part {n}" for n in range(lo, hi + 1) if n not in have]


def read_uncompressed_size(path: str, key: str) -> int | None:
    """Cheaply read the stored uncompressed size of a single-stream file.

    gzip stores it (mod 2^32) in the last 4 bytes; zstd may store it in the
    frame header. Other formats don't record it at all, so we return None and
    the UI shows "Unknown" rather than a wrong number.
    """
    try:
        if key in ("gz", "tar.gz"):
            size = os.path.getsize(path)
            if size < 18:
                return None
            with open(path, "rb") as fh:
                fh.seek(-4, os.SEEK_END)
                return struct.unpack("<I", fh.read(4))[0]
    except OSError:
        return None
    return None
