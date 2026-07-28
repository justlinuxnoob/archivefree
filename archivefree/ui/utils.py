"""Formatting and icon helpers for the views."""

from __future__ import annotations

import datetime

from gi.repository import Gio, GLib

_UNITS = ("bytes", "kB", "MB", "GB", "TB", "PB")

#: Icon names chosen per file kind. GTK falls back through these, so we give a
#: specific name first and a generic one after.
_ICON_BY_KIND = {
    "folder": "folder-symbolic",
    "archive": "package-x-generic-symbolic",
    "image": "image-x-generic-symbolic",
    "audio": "audio-x-generic-symbolic",
    "video": "video-x-generic-symbolic",
    "text": "text-x-generic-symbolic",
    "code": "text-x-script-symbolic",
    "pdf": "x-office-document-symbolic",
    "font": "font-x-generic-symbolic",
    "executable": "application-x-executable-symbolic",
    "file": "text-x-generic-symbolic",
}

_EXT_KIND = {
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "bmp": "image",
    "svg": "image", "webp": "image", "ico": "image", "tiff": "image", "avif": "image",
    "mp3": "audio", "flac": "audio", "ogg": "audio", "wav": "audio", "m4a": "audio",
    "opus": "audio", "aac": "audio",
    "mp4": "video", "mkv": "video", "avi": "video", "webm": "video", "mov": "video",
    "pdf": "pdf",
    "ttf": "font", "otf": "font", "woff": "font", "woff2": "font",
    "py": "code", "js": "code", "ts": "code", "c": "code", "h": "code", "cpp": "code",
    "rs": "code", "go": "code", "sh": "code", "rb": "code", "java": "code",
    "json": "code", "xml": "code", "yaml": "code", "yml": "code", "toml": "code",
    "html": "code", "css": "code", "sql": "code",
    "txt": "text", "md": "text", "log": "text", "csv": "text", "ini": "text",
    "conf": "text", "cfg": "text",
    "zip": "archive", "7z": "archive", "rar": "archive", "tar": "archive",
    "gz": "archive", "bz2": "archive", "xz": "archive", "zst": "archive",
    "iso": "archive", "deb": "archive", "rpm": "archive",
    "appimage": "executable", "bin": "executable", "run": "executable",
}


def format_size(size: int) -> str:
    """Human-readable size. ``-1`` means unknown, which we show as an em dash."""
    if size < 0:
        return "—"
    if size == 0:
        return "0 bytes"
    if size < 1000:
        return f"{size} bytes"
    value = float(size)
    for unit in _UNITS[1:]:
        value /= 1000.0
        if value < 1000.0:
            precision = 1 if value < 10 else 0
            return f"{value:.{precision}f} {unit}"
    return f"{value:.1f} PB"


def format_count(count: int, singular: str = "item", plural: str | None = None) -> str:
    plural = plural or singular + "s"
    return f"{count:,} {singular if count == 1 else plural}"


def format_date(when: datetime.datetime | None) -> str:
    """Dates from today show a time; this year, no year; older, in full."""
    if when is None:
        return "—"
    now = datetime.datetime.now()
    if when.date() == now.date():
        return when.strftime("%H:%M")
    if when.year == now.year:
        return when.strftime("%-d %b %H:%M")
    return when.strftime("%-d %b %Y")


def format_date_long(when: datetime.datetime | None) -> str:
    return when.strftime("%A, %-d %B %Y at %H:%M") if when else "Unknown"


def kind_of(name: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    _, _, ext = name.rpartition(".")
    return _EXT_KIND.get(ext.lower(), "file") if ext and ext != name else "file"


def icon_name(name: str, is_dir: bool) -> str:
    return _ICON_BY_KIND[kind_of(name, is_dir)]


def describe_type(name: str, is_dir: bool) -> str:
    """A human label for the Type column, from the shared MIME database."""
    if is_dir:
        return "Folder"
    content_type, _ = Gio.content_type_guess(name, None)
    if content_type and not content_type.endswith("application/octet-stream"):
        description = Gio.content_type_get_description(content_type)
        if description and description.lower() != "unknown":
            return description
    _, _, ext = name.rpartition(".")
    if ext and ext != name:
        return f"{ext.upper()} file"
    return "File"


def is_probably_text(data: bytes) -> bool:
    """Heuristic used to decide whether a preview can be shown as text."""
    if not data:
        return True
    if b"\x00" in data[:8192]:
        return False
    sample = data[:8192]
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        # A multi-byte character truncated at the boundary is still text.
        try:
            sample[:-4].decode("utf-8")
            return True
        except UnicodeDecodeError:
            pass
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(sample) > 0.85


def shorten_path(path: str, max_length: int = 48) -> str:
    """Middle-truncate a path for display in a subtitle."""
    if len(path) <= max_length:
        return path
    head = max_length // 2 - 2
    return f"{path[:head]}…{path[-(max_length - head - 1):]}"


def home_relative(path: str) -> str:
    """Show ``/home/you/Downloads`` as ``Home / Downloads``."""
    home = GLib.get_home_dir()
    if path == home:
        return "Home"
    if home and path.startswith(home + "/"):
        return "Home / " + path[len(home) + 1:].replace("/", " / ")
    return path.replace("/", " / ").strip(" /")
