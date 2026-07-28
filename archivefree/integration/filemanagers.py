"""Right-click menu entries for the common Linux file managers.

Linux file managers share no menu-extension standard, so this module speaks each
one's own dialect:

* **Thunar** — entries merged into ``~/.config/Thunar/uca.xml``
* **Nemo** — ``.nemo_action`` files
* **Dolphin** — KIO service menus
* **Caja / PCManFM / Nautilus-Actions** — DES-EMA ``.desktop`` action files
* **Nautilus** — executable scripts in its scripts folder

Everything is written under the user's home, so nothing needs root and
:func:`uninstall_all` removes exactly what we added. The packaged builds also
drop the declarative files (Nemo, Dolphin, DES-EMA) into ``/usr/share``, so for
most people this is only needed for Thunar and Nautilus.
"""

from __future__ import annotations

import os
import stat
import xml.etree.ElementTree as ET

from .._version import APP_ID

#: Patterns used by the file managers that match on filename rather than MIME.
ARCHIVE_PATTERNS = [
    "*.zip", "*.7z", "*.rar", "*.tar", "*.tar.gz", "*.tgz", "*.tar.bz2", "*.tbz2",
    "*.tar.xz", "*.txz", "*.tar.zst", "*.tzst", "*.gz", "*.bz2", "*.xz", "*.zst",
    "*.iso", "*.cab", "*.lzma", "*.lz4", "*.lha", "*.arj", "*.cpio", "*.deb", "*.rpm",
]

_MIME_LIST = (
    "application/zip;application/x-7z-compressed;application/vnd.rar;"
    "application/x-tar;application/gzip;application/x-compressed-tar;"
    "application/x-bzip2;application/x-bzip2-compressed-tar;application/x-xz;"
    "application/x-xz-compressed-tar;application/zstd;"
    "application/x-zstd-compressed-tar;application/x-cd-image;"
    "application/vnd.ms-cab-compressed;"
)

_UNIQUE_PREFIX = "archivefree-"


def _data_home() -> str:
    # Host paths, not the sandbox's: menu entries are read by the file manager
    # running on the host, so writing them into ~/.var/app/… would do nothing.
    from .defaults import _host_data_dir

    return _host_data_dir()


def _config_home() -> str:
    from .defaults import _host_config_dir

    return _host_config_dir()


def _command() -> str:
    """The command file managers should run.

    Inside a Flatpak the binary isn't on the host's PATH, so the menu entries
    have to go back through ``flatpak run``.
    """
    if os.path.exists("/.flatpak-info"):
        return f"flatpak run {APP_ID}"
    return "archivefree"


# ---------------------------------------------------------------------------
# Thunar
# ---------------------------------------------------------------------------

_THUNAR_ACTIONS = [
    {
        "unique-id": _UNIQUE_PREFIX + "open",
        "name": "Open with ArchiveFree",
        "description": "Look inside this archive before unpacking it",
        "icon": APP_ID,
        "command": "{cmd} %f",
        "patterns": ";".join(ARCHIVE_PATTERNS),
        "types": ["other-files"],
    },
    {
        "unique-id": _UNIQUE_PREFIX + "extract-here",
        "name": "Extract Here",
        "description": "Extract this archive into a folder beside it",
        "icon": "archive-extract",
        "command": "{cmd} --extract-here %F",
        "patterns": ";".join(ARCHIVE_PATTERNS),
        "types": ["other-files"],
    },
    {
        "unique-id": _UNIQUE_PREFIX + "compress",
        "name": "Compress…",
        "description": "Create a new archive from the selected items",
        "icon": APP_ID,
        "command": "{cmd} --new-archive %F",
        "patterns": "*",
        "types": ["directories", "audio-files", "image-files", "other-files",
                  "text-files", "video-files"],
    },
]


def thunar_available() -> bool:
    import shutil

    return shutil.which("thunar") is not None


def install_thunar() -> bool:
    """Merge our actions into Thunar's uca.xml, leaving the user's own alone."""
    path = os.path.join(_config_home(), "Thunar", "uca.xml")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            tree = ET.parse(path)
            root = tree.getroot()
        else:
            root = ET.Element("actions")
            tree = ET.ElementTree(root)

        # Drop any previous version of our entries so re-running is idempotent.
        for action in list(root.findall("action")):
            unique = action.findtext("unique-id", "")
            if unique.startswith(_UNIQUE_PREFIX):
                root.remove(action)

        command = _command()
        for spec in _THUNAR_ACTIONS:
            action = ET.SubElement(root, "action")
            ET.SubElement(action, "icon").text = spec["icon"]
            ET.SubElement(action, "name").text = spec["name"]
            ET.SubElement(action, "unique-id").text = spec["unique-id"]
            ET.SubElement(action, "command").text = spec["command"].format(cmd=command)
            ET.SubElement(action, "description").text = spec["description"]
            ET.SubElement(action, "patterns").text = spec["patterns"]
            for kind in spec["types"]:
                ET.SubElement(action, kind)

        _indent(root)
        tree.write(path, encoding="UTF-8", xml_declaration=True)
        return True
    except (OSError, ET.ParseError):
        return False


def uninstall_thunar() -> bool:
    path = os.path.join(_config_home(), "Thunar", "uca.xml")
    if not os.path.exists(path):
        return True
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        removed = False
        for action in list(root.findall("action")):
            if action.findtext("unique-id", "").startswith(_UNIQUE_PREFIX):
                root.remove(action)
                removed = True
        if removed:
            _indent(root)
            tree.write(path, encoding="UTF-8", xml_declaration=True)
        return True
    except (OSError, ET.ParseError):
        return False


def _indent(element: ET.Element, level: int = 0) -> None:
    """Pretty-print in place, so a human editing uca.xml later isn't punished."""
    pad = "\n" + "\t" * level
    if len(element):
        if not (element.text or "").strip():
            element.text = pad + "\t"
        for child in element:
            _indent(child, level + 1)
        if not (element.tail or "").strip():
            element.tail = pad
        if not (element[-1].tail or "").strip():
            element[-1].tail = pad
    elif level and not (element.tail or "").strip():
        element.tail = pad


# ---------------------------------------------------------------------------
# Nautilus / Caja scripts
# ---------------------------------------------------------------------------

_SCRIPT_TEMPLATE = """#!/bin/sh
# Installed by ArchiveFree. Safe to delete.
IFS='
'
set -- $%(variable)s
exec %(command)s %(flag)s"$@"
"""

_SCRIPTS = [
    ("Open with ArchiveFree", ""),
    ("Extract Here", "--extract-here "),
    ("Compress with ArchiveFree", "--new-archive "),
]


def _script_dirs() -> list[tuple[str, str]]:
    """(scripts directory, environment variable holding the selection)."""
    return [
        (os.path.join(_data_home(), "nautilus", "scripts"),
         "NAUTILUS_SCRIPT_SELECTED_FILE_PATHS"),
        (os.path.join(_config_home(), "caja", "scripts"),
         "CAJA_SCRIPT_SELECTED_FILE_PATHS"),
    ]


def install_scripts() -> bool:
    ok = True
    command = _command()
    for directory, variable in _script_dirs():
        try:
            os.makedirs(directory, exist_ok=True)
            for name, flag in _SCRIPTS:
                path = os.path.join(directory, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(_SCRIPT_TEMPLATE % {
                        "variable": variable, "command": command, "flag": flag,
                    })
                os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP
                         | stat.S_IXOTH)
        except OSError:
            ok = False
    return ok


def uninstall_scripts() -> bool:
    for directory, _ in _script_dirs():
        for name, _flag in _SCRIPTS:
            try:
                path = os.path.join(directory, name)
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                return False
    return True


# ---------------------------------------------------------------------------
# Declarative files (Nemo, Dolphin, DES-EMA)
# ---------------------------------------------------------------------------

def _nemo_actions() -> dict[str, str]:
    command = _command()
    return {
        "archivefree-extract-here.nemo_action": f"""[Nemo Action]
Name=Extract Here with ArchiveFree
Comment=Extract this archive into a folder beside it
Exec={command} --extract-here %F
Icon-Name={APP_ID}
Selection=NotNone
Extensions=zip;7z;rar;tar;gz;tgz;bz2;tbz2;xz;txz;zst;tzst;iso;cab;
Quote=double
""",
        "archivefree-compress.nemo_action": f"""[Nemo Action]
Name=Compress with ArchiveFree…
Comment=Create a new archive from the selected items
Exec={command} --new-archive %F
Icon-Name={APP_ID}
Selection=NotNone
Extensions=any;
Quote=double
""",
    }


def _dolphin_servicemenu() -> str:
    command = _command()
    return f"""[Desktop Entry]
Type=Service
ServiceTypes=KonqPopupMenu/Plugin
MimeType={_MIME_LIST}
Actions=archivefreeOpen;archivefreeExtractHere;
X-KDE-Priority=TopLevel
Icon={APP_ID}

[Desktop Action archivefreeOpen]
Name=Open with ArchiveFree
Icon={APP_ID}
Exec={command} %F

[Desktop Action archivefreeExtractHere]
Name=Extract Here
Icon=archive-extract
Exec={command} --extract-here %F
"""


def _desema_actions() -> dict[str, str]:
    """Actions understood by Caja, PCManFM and Nautilus-Actions."""
    command = _command()
    return {
        "archivefree-extract-here.desktop": f"""[Desktop Entry]
Type=Action
Name=Extract Here with ArchiveFree
Icon={APP_ID}
Profiles=extract;

[X-Action-Profile extract]
MimeTypes={_MIME_LIST}
Exec={command} --extract-here %F
""",
        "archivefree-compress.desktop": f"""[Desktop Entry]
Type=Action
Name=Compress with ArchiveFree…
Icon={APP_ID}
Profiles=compress;

[X-Action-Profile compress]
MimeTypes=*/*;
Exec={command} --new-archive %F
""",
    }


def _declarative_targets() -> list[tuple[str, dict[str, str]]]:
    return [
        (os.path.join(_data_home(), "nemo", "actions"), _nemo_actions()),
        (os.path.join(_data_home(), "kio", "servicemenus"),
         {"archivefree.desktop": _dolphin_servicemenu()}),
        (os.path.join(_data_home(), "kservices5", "ServiceMenus"),
         {"archivefree.desktop": _dolphin_servicemenu()}),
        (os.path.join(_data_home(), "file-manager", "actions"), _desema_actions()),
    ]


def install_declarative() -> bool:
    ok = True
    for directory, files in _declarative_targets():
        try:
            os.makedirs(directory, exist_ok=True)
            for name, content in files.items():
                path = os.path.join(directory, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                if name.endswith(".desktop"):
                    os.chmod(path, 0o755)
        except OSError:
            ok = False
    return ok


def uninstall_declarative() -> bool:
    ok = True
    for directory, files in _declarative_targets():
        for name in files:
            try:
                path = os.path.join(directory, name)
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                ok = False
    return ok


# ---------------------------------------------------------------------------

def install_all() -> dict[str, bool]:
    """Install every integration. Returns a per-target success map."""
    return {
        "Thunar": install_thunar(),
        "Nemo, Dolphin, Caja and PCManFM": install_declarative(),
        "Nautilus and Caja scripts": install_scripts(),
    }


def uninstall_all() -> dict[str, bool]:
    return {
        "Thunar": uninstall_thunar(),
        "Nemo, Dolphin, Caja and PCManFM": uninstall_declarative(),
        "Nautilus and Caja scripts": uninstall_scripts(),
    }


def is_installed() -> bool:
    """True if our Thunar entry or our Nemo action is present."""
    thunar = os.path.join(_config_home(), "Thunar", "uca.xml")
    if os.path.exists(thunar):
        try:
            with open(thunar, encoding="utf-8") as fh:
                if _UNIQUE_PREFIX in fh.read():
                    return True
        except OSError:
            pass
    nemo = os.path.join(_data_home(), "nemo", "actions",
                        "archivefree-extract-here.nemo_action")
    return os.path.exists(nemo)


def detected_file_managers() -> list[str]:
    """Which file managers are actually installed, for an honest settings page."""
    import shutil

    found = []
    for binary, label in [("thunar", "Thunar"), ("nautilus", "Files (Nautilus)"),
                          ("nemo", "Nemo"), ("caja", "Caja"), ("dolphin", "Dolphin"),
                          ("pcmanfm", "PCManFM"), ("pcmanfm-qt", "PCManFM-Qt")]:
        if shutil.which(binary):
            found.append(label)
    return found
