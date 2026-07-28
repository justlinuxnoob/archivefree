"""Top-level right-click entries for Nautilus, Nemo and Caja.

Installed as a python extension rather than a shell script. Scripts work, but
every file manager buries them in a "Scripts ▸" submenu, which users reported as
effectively undiscoverable — you have to know to go looking. An extension puts
"Extract Here" and "Compress…" directly in the menu where people expect them.

The same file serves all three file managers: they expose near-identical
MenuProvider APIs under different namespaces, so we detect which host loaded us
and bind to that. Nautilus 43+ moved to the 4.0 API, older builds use 3.0.
"""

import os
import subprocess

import gi

# Work out which file manager is loading us. The first namespace that imports
# is the host; there is no other way to ask.
_HOST = None
_NS = None

for _namespace, _versions in (
    ("Nautilus", ("4.0", "3.0")),
    ("Nemo", ("3.0",)),
    ("Caja", ("2.0",)),
):
    for _version in _versions:
        try:
            gi.require_version(_namespace, _version)
            _NS = __import__("gi.repository", fromlist=[_namespace])
            _HOST = getattr(_NS, _namespace)
            break
        except (ValueError, ImportError, AttributeError):
            continue
    if _HOST is not None:
        break

from gi.repository import GObject  # noqa: E402

APP_ID = "io.github.justlinuxnoob.ArchiveFree"

ARCHIVE_MIME_TYPES = {
    "application/zip", "application/x-7z-compressed", "application/vnd.rar",
    "application/x-rar", "application/x-rar-compressed", "application/x-tar",
    "application/gzip", "application/x-gzip", "application/x-compressed-tar",
    "application/x-bzip", "application/x-bzip2",
    "application/x-bzip-compressed-tar", "application/x-bzip2-compressed-tar",
    "application/x-xz", "application/x-xz-compressed-tar",
    "application/zstd", "application/x-zstd-compressed-tar",
    "application/x-lzma", "application/x-lzma-compressed-tar",
    "application/x-lz4", "application/x-cd-image",
    "application/vnd.ms-cab-compressed", "application/x-cpio",
    "application/x-lha", "application/x-lzh-compressed", "application/x-archive",
    "application/x-xar", "application/x-ms-wim", "application/x-apple-diskimage",
}

ARCHIVE_SUFFIXES = (
    ".zip", ".7z", ".rar", ".tar", ".tgz", ".tbz2", ".txz", ".tzst",
    ".gz", ".bz2", ".xz", ".zst", ".lz4", ".lzma", ".iso", ".cab",
)


def _command():
    """How to launch ArchiveFree — through Flatpak when that's how it's installed."""
    for candidate in ("/usr/bin/archivefree", "/usr/local/bin/archivefree"):
        if os.path.exists(candidate):
            return [candidate]
    flatpak_export = os.path.expanduser(
        f"~/.local/share/flatpak/exports/bin/{APP_ID}")
    if os.path.exists(flatpak_export) or os.path.exists(
            f"/var/lib/flatpak/exports/bin/{APP_ID}"):
        return ["flatpak", "run", APP_ID]
    return ["archivefree"]


def _launch(args, paths):
    argv = _command() + args + list(paths)
    subprocess.Popen(argv, start_new_session=True)


def _is_archive(file_info):
    if file_info.get_uri_scheme() != "file":
        return False
    if file_info.is_directory():
        return False
    if file_info.get_mime_type() in ARCHIVE_MIME_TYPES:
        return True
    return file_info.get_name().lower().endswith(ARCHIVE_SUFFIXES)


def _paths(files):
    out = []
    for item in files:
        location = item.get_location()
        path = location.get_path() if location else None
        if path:
            out.append(path)
    return out


def _menu_item(name, label, tip):
    return _HOST.MenuItem(name=name, label=label, tip=tip)


class ArchiveFreeMenuProvider(GObject.GObject, _HOST.MenuProvider):
    """Adds ArchiveFree entries to the file manager's context menu."""

    def get_file_items(self, *args):
        # Nautilus 4.0 passes (files); 3.0 and Nemo/Caja pass (window, files).
        files = args[-1]
        if not files:
            return []
        paths = _paths(files)
        if not paths:
            return []

        items = []
        archives = [f for f in files if _is_archive(f)]

        if archives and len(archives) == len(files):
            archive_paths = _paths(archives)

            open_item = _menu_item(
                "ArchiveFree::open", "Open with ArchiveFree",
                "Look inside this archive before unpacking it")
            open_item.connect("activate", lambda *_: _launch([], archive_paths))
            items.append(open_item)

            here = _menu_item(
                "ArchiveFree::extract_here", "Extract Here",
                "Extract into a folder beside the archive")
            here.connect("activate",
                         lambda *_: _launch(["--extract-here"], archive_paths))
            items.append(here)

        compress = _menu_item(
            "ArchiveFree::compress", "Compress…",
            "Create a new archive from the selected items")
        compress.connect("activate", lambda *_: _launch(["--new-archive"], paths))
        items.append(compress)
        return items

    def get_background_items(self, *args):
        folder = args[-1]
        location = folder.get_location() if folder else None
        path = location.get_path() if location else None
        if not path:
            return []
        item = _menu_item(
            "ArchiveFree::compress_folder", "Compress This Folder…",
            "Create a new archive from this folder")
        item.connect("activate", lambda *_: _launch(["--new-archive"], [path]))
        return [item]


# Some hosts look for a differently named class; alias for safety.
if _HOST is not None:
    ArchiveFreeMenu = ArchiveFreeMenuProvider
