"""Making ArchiveFree the default handler for archive files — and undoing it.

Everything here writes to ``~/.config/mimeapps.list`` through ``xdg-mime`` (or
directly, as a fallback). Nothing needs root, nothing touches system files, and
:func:`unset_as_default` puts back exactly what was there before, because we
record the previous handler for each type the first time we change it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from .._version import APP_ID

DESKTOP_FILE = f"{APP_ID}.desktop"

#: The MIME types we offer to take over. Deliberately excludes types where
#: hijacking the default would be unhelpful — .deb and .rpm should keep opening
#: in the package installer, not in an archive browser.
HANDLED_TYPES = [
    "application/zip",
    "application/x-7z-compressed",
    "application/vnd.rar",
    "application/x-rar",
    "application/x-rar-compressed",
    "application/x-tar",
    "application/gzip",
    "application/x-gzip",
    "application/x-compressed-tar",
    "application/x-bzip2",
    "application/x-bzip",
    "application/x-bzip-compressed-tar",
    "application/x-bzip2-compressed-tar",
    "application/x-xz",
    "application/x-xz-compressed-tar",
    "application/zstd",
    "application/x-zstd-compressed-tar",
    "application/x-lzma",
    "application/x-lzma-compressed-tar",
    "application/x-lz4",
    "application/x-cd-image",
    "application/vnd.ms-cab-compressed",
    "application/x-cpio",
    "application/x-lha",
    "application/x-lzh-compressed",
    "application/x-archive",
    "application/x-xar",
    "application/x-ms-wim",
    "application/x-apple-diskimage",
    # Comic books have no default handler on a stock desktop, so taking these
    # over is a clear win rather than a hijack.
    "application/vnd.comicbook+zip",
    "application/vnd.comicbook-rar",
    "application/x-cb7",
]

#: Deliberately *not* in the list above. ArchiveFree can open these — they are
#: ZIP containers — and appears under "Open With", but an .epub belongs to an
#: e-book reader and a .docx to an office suite. Becoming the default for them
#: would be a hijack, not a feature.
NOT_CLAIMED_BY_DEFAULT = [
    "application/epub+zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text",
    "application/java-archive",
    "application/vnd.android.package-archive",
]


def in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def _home_dir() -> str:
    """The user's home directory, without needing PyGObject to ask."""
    return os.path.expanduser("~")


def _host_config_dir() -> str:
    """The *host's* config directory, even when we're inside a Flatpak.

    Flatpak points ``XDG_CONFIG_HOME`` at the app's private
    ``~/.var/app/<id>/config``. Writing mimeapps.list there would change
    nothing at all on the desktop — the association has to go in the real
    ``~/.config``, which ``--filesystem=host`` makes reachable at its true path.
    """
    if in_flatpak():
        return os.path.join(_home_dir(), ".config")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home_dir(), ".config")


def _host_data_dir() -> str:
    if in_flatpak():
        return os.path.join(_home_dir(), ".local", "share")
    return os.environ.get("XDG_DATA_HOME") or os.path.join(
        _home_dir(), ".local", "share")


#: Where we remember what was default before we changed anything.
def _backup_path() -> str:
    return os.path.join(_host_data_dir(), "archivefree", "previous-handlers.json")


def _xdg_mime() -> str | None:
    # Inside the sandbox xdg-mime would edit the app's private config, so we
    # always fall through to writing the host's mimeapps.list ourselves.
    if in_flatpak():
        return None
    return shutil.which("xdg-mime")


def _read_mimeapps_default(mime_type: str) -> str | None:
    """Read one entry straight out of the host's mimeapps.list."""
    path = _mimeapps_path()
    if not os.path.exists(path):
        return None
    from gi.repository import GLib

    try:
        keyfile = GLib.KeyFile()
        keyfile.load_from_file(path, GLib.KeyFileFlags.NONE)
        value = keyfile.get_string("Default Applications", mime_type)
        # The value may be a semicolon-separated list; the first is the default.
        return value.split(";")[0].strip() or None
    except GLib.Error:
        return None


def _host_application_dirs() -> list[str]:
    """Every directory the host looks in for .desktop files.

    Inside a Flatpak the runtime's own ``/usr`` is *not* the host's, but
    ``--filesystem=host`` exposes the real one at ``/run/host/usr``, which is
    how we can still see what the user has installed.
    """
    dirs = [os.path.join(_host_data_dir(), "applications")]
    if in_flatpak():
        dirs.append("/run/host/usr/share/applications")
        dirs.append("/run/host/usr/local/share/applications")
    else:
        dirs.append("/usr/share/applications")
        dirs.append("/usr/local/share/applications")
    home = _home_dir()
    dirs.append(os.path.join(home, ".local/share/flatpak/exports/share/applications"))
    dirs.append("/var/lib/flatpak/exports/share/applications")
    if in_flatpak():
        dirs.append("/run/host/var/lib/flatpak/exports/share/applications")
    return dirs


def registered_handlers(mime_type: str) -> list[str]:
    """Other applications on the host that can open ``mime_type``.

    Read out of each directory's ``mimeinfo.cache``, in the order the desktop
    itself would consult them. Our own entry is filtered out, so this answers
    "what would open this if ArchiveFree weren't here?".
    """
    from gi.repository import GLib

    found: list[str] = []
    for directory in _host_application_dirs():
        cache = os.path.join(directory, "mimeinfo.cache")
        if not os.path.exists(cache):
            continue
        try:
            keyfile = GLib.KeyFile()
            keyfile.load_from_file(cache, GLib.KeyFileFlags.NONE)
            value = keyfile.get_string("MIME Cache", mime_type)
        except GLib.Error:
            continue
        for entry in value.split(";"):
            entry = entry.strip()
            if entry and entry != DESKTOP_FILE and entry not in found:
                found.append(entry)
    return found


def current_default(mime_type: str) -> str | None:
    """The .desktop file currently handling ``mime_type``, if any.

    The host's mimeapps.list is authoritative and is checked first, because
    inside a Flatpak ``Gio.AppInfo`` only ever sees the sandbox's own view.
    """
    explicit = _read_mimeapps_default(mime_type)
    if explicit:
        return explicit

    if not in_flatpak():
        from gi.repository import Gio

        app_info = Gio.AppInfo.get_default_for_type(mime_type, False)
        if app_info is not None:
            return app_info.get_id()
        exe = _xdg_mime()
        if exe:
            try:
                result = subprocess.run([exe, "query", "default", mime_type],
                                        capture_output=True, text=True, timeout=5)
                return result.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                pass
        return None

    # In a Flatpak there is no explicit entry to read and no host-aware
    # AppInfo, so fall back to whatever the host has registered.
    handlers = registered_handlers(mime_type)
    return handlers[0] if handlers else None


def is_default(mime_type: str | None = None) -> bool:
    """True when ArchiveFree handles ``mime_type`` (or a clear majority of types)."""
    if mime_type is not None:
        return current_default(mime_type) == DESKTOP_FILE
    handled = sum(1 for t in HANDLED_TYPES if current_default(t) == DESKTOP_FILE)
    return handled >= max(3, len(HANDLED_TYPES) // 2)


def status() -> tuple[int, int]:
    """(types we handle, types we offered to handle) — for the settings UI."""
    handled = sum(1 for t in HANDLED_TYPES if current_default(t) == DESKTOP_FILE)
    return handled, len(HANDLED_TYPES)


def is_installed() -> bool:
    """True when our .desktop file is visible to the desktop environment.

    Setting a default for a .desktop that isn't installed silently does nothing,
    so the UI checks this before offering.
    """
    if in_flatpak():
        # Flatpak exports our .desktop to the host; inside the sandbox we can
        # see the same file under the app's own share directory.
        return os.path.exists(f"/app/share/applications/{DESKTOP_FILE}")
    from gi.repository import Gio

    try:
        # PyGObject raises rather than returning None when the file is absent.
        return Gio.DesktopAppInfo.new(DESKTOP_FILE) is not None
    except TypeError:
        return False


def set_as_default() -> tuple[int, list[str]]:
    """Become the default for every handled type.

    Returns ``(types changed, warnings)``. Never raises: a desktop that refuses
    one MIME type shouldn't abort the rest.
    """
    previous = _load_backup()
    exe = _xdg_mime()
    changed = 0
    warnings: list[str] = []

    for mime_type in HANDLED_TYPES:
        existing = current_default(mime_type)
        if existing == DESKTOP_FILE:
            continue
        # Remember the first handler we displaced, so undo is faithful.
        if mime_type not in previous:
            previous[mime_type] = existing or ""

        if _set_one(mime_type, DESKTOP_FILE, exe):
            changed += 1
        else:
            warnings.append(mime_type)

    _save_backup(previous)
    _refresh_desktop_database()
    return changed, warnings


def unset_as_default() -> int:
    """Hand every type back to whatever handled it before. Returns types restored."""
    previous = _load_backup()
    exe = _xdg_mime()
    restored = 0

    for mime_type in HANDLED_TYPES:
        if current_default(mime_type) != DESKTOP_FILE:
            continue
        original = previous.get(mime_type, "")
        if not original:
            # No explicit default before us, but another application may still
            # have been handling it by fallback. Simply deleting our line would
            # not bring that back: our .desktop lives in a directory the desktop
            # ranks *above* /usr/share, so we would silently keep winning. Name
            # the old handler explicitly instead — that is what "undo" means to
            # the person who clicked it.
            others = registered_handlers(mime_type)
            original = others[0] if others else ""

        if original:
            if _set_one(mime_type, original, exe):
                restored += 1
        elif _remove_association(mime_type):
            # Genuinely nothing else can open it; drop our claim entirely.
            restored += 1

    _save_backup({})
    _refresh_desktop_database()
    return restored


# -- the actual writing --------------------------------------------------


def _set_one(mime_type: str, desktop_file: str, exe: str | None) -> bool:
    if exe:
        try:
            result = subprocess.run([exe, "default", desktop_file, mime_type],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    return _write_mimeapps(mime_type, desktop_file)


def _mimeapps_path() -> str:
    return os.path.join(_host_config_dir(), "mimeapps.list")


def _write_mimeapps(mime_type: str, desktop_file: str) -> bool:
    """Edit ~/.config/mimeapps.list directly, preserving everything else in it."""
    from gi.repository import GLib

    path = _mimeapps_path()
    try:
        keyfile = GLib.KeyFile()
        if os.path.exists(path):
            try:
                keyfile.load_from_file(path, GLib.KeyFileFlags.KEEP_COMMENTS
                                       | GLib.KeyFileFlags.KEEP_TRANSLATIONS)
            except GLib.Error:
                pass
        keyfile.set_string("Default Applications", mime_type, desktop_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        keyfile.save_to_file(path)
        return True
    except (GLib.Error, OSError):
        return False


def _remove_association(mime_type: str) -> bool:
    from gi.repository import GLib

    path = _mimeapps_path()
    if not os.path.exists(path):
        return False
    try:
        keyfile = GLib.KeyFile()
        keyfile.load_from_file(path, GLib.KeyFileFlags.KEEP_COMMENTS
                               | GLib.KeyFileFlags.KEEP_TRANSLATIONS)
        try:
            keyfile.remove_key("Default Applications", mime_type)
        except GLib.Error:
            return False
        keyfile.save_to_file(path)
        return True
    except (GLib.Error, OSError):
        return False


def _refresh_desktop_database() -> None:
    """Nudge the desktop to notice the change without a logout.

    Pointless inside a Flatpak — the sandbox has no way to refresh the host's
    caches — but harmless, and the mimeapps.list write is what actually counts.
    """
    if in_flatpak():
        return
    exe = shutil.which("update-desktop-database")
    if not exe:
        return
    applications = os.path.join(_host_data_dir(), "applications")
    if os.path.isdir(applications):
        try:
            subprocess.run([exe, applications], capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass


def _load_backup() -> dict[str, str]:
    try:
        with open(_backup_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_backup(data: dict[str, str]) -> None:
    try:
        path = _backup_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
    except OSError:
        pass
