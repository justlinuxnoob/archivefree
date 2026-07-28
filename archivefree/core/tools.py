"""Discovery of the external helper programs backends shell out to.

Distributions disagree wildly about what the 7-Zip binary is called (7z, 7zz,
7za, 7zr) and which package provides it, so this module probes once and caches
the answer. When a tool is genuinely absent we raise :class:`MissingTool`, which
names the apt package — that turns "backend tooling is missing" from a dead end
into a one-line fix.
"""

from __future__ import annotations

import functools
import shutil
import subprocess

from .errors import MissingTool

# Ordered by preference. 7zz/7z handle every format; 7za is the reduced build
# that still covers 7z/zip/tar; 7zr only does 7z.
_SEVENZIP_CANDIDATES = ("7zz", "7z", "7za", "7zr")

_APT_PACKAGES = {
    "7z": "7zip",
    "unrar": "unrar-free   (or 'unrar' from non-free for full RAR5 support)",
    "zstd": "zstd",
    "lz4": "lz4",
    "lzip": "lzip",
    "cabextract": "cabextract",
}


@functools.lru_cache(maxsize=1)
def sevenzip() -> str | None:
    """Path to the best available 7-Zip binary, or None."""
    for name in _SEVENZIP_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


@functools.lru_cache(maxsize=1)
def sevenzip_is_full() -> bool:
    """True when the 7-Zip binary can read RAR/ISO/CAB, not just 7z/zip."""
    exe = sevenzip()
    if not exe:
        return False
    return not exe.endswith(("7zr", "7za"))


@functools.cache
def which(name: str) -> str | None:
    return shutil.which(name)


def require(name: str, purpose: str) -> str:
    """Return the path to ``name``, or raise a helpful :class:`MissingTool`."""
    if name == "7z":
        exe = sevenzip()
        if exe:
            return exe
        raise MissingTool("7z", purpose, _APT_PACKAGES["7z"])
    path = which(name)
    if path:
        return path
    raise MissingTool(name, purpose, _APT_PACKAGES.get(name, name))


@functools.lru_cache(maxsize=1)
def sevenzip_version() -> str:
    exe = sevenzip()
    if not exe:
        return ""
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if "7-Zip" in line:
                return line.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def available_report() -> list[tuple[str, str, bool]]:
    """(tool, what it enables, present?) — used by the About > Backends page."""
    return [
        ("7-Zip", "7z, RAR, ISO, CAB, DMG and encrypted ZIP", sevenzip() is not None),
        ("unrar", "RAR archives (including RAR5 and encrypted)", which("unrar") is not None),
        ("zstd", "Zstandard (.zst) archives", which("zstd") is not None),
        ("lz4", "LZ4 (.lz4) archives", which("lz4") is not None),
    ]
