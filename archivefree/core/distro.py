"""Working out which distribution we're on, so install advice is actually usable.

The one message in this app that a user is expected to *act* on is "a helper
program is missing, here is how to install it". Telling a Fedora user to run
``sudo apt install`` turns that from a fix into a dead end, so this module
resolves both the package manager and the right package name per distribution.

Detection reads ``/etc/os-release``. Inside a Flatpak that file describes the
runtime rather than the user's system, so we read the host's copy under
``/run/host`` instead.
"""

from __future__ import annotations

import functools
import os

#: Per-family install command, formatted with the package list.
_COMMANDS = {
    "debian": "sudo apt install {packages}",
    "fedora": "sudo dnf install {packages}",
    "arch": "sudo pacman -S {packages}",
    "suse": "sudo zypper install {packages}",
    "alpine": "sudo apk add {packages}",
    "gentoo": "sudo emerge {packages}",
    "void": "sudo xbps-install -S {packages}",
}

#: Package names differ per family. Keyed by our internal tool name.
_PACKAGES = {
    "7z": {
        "debian": "7zip",
        "fedora": "p7zip p7zip-plugins",
        "arch": "7zip",
        "suse": "7zip",
        "alpine": "7zip",
        "gentoo": "app-arch/7zip",
        "void": "7zip",
    },
    "unrar": {
        "debian": "unrar-free",
        "fedora": "unrar",
        "arch": "unrar",
        "suse": "unrar",
        "alpine": "unrar",
        "gentoo": "app-arch/unrar",
        "void": "unrar",
    },
    "zstd": dict.fromkeys(_COMMANDS, "zstd") | {"gentoo": "app-arch/zstd"},
    "lz4": dict.fromkeys(_COMMANDS, "lz4") | {"gentoo": "app-arch/lz4"},
    "lzip": dict.fromkeys(_COMMANDS, "lzip") | {"gentoo": "app-arch/lzip"},
}

#: os-release IDs that belong to a family but don't say so in ID_LIKE.
_ALIASES = {
    "ubuntu": "debian", "linuxmint": "debian", "mx": "debian", "pop": "debian",
    "elementary": "debian", "zorin": "debian", "kali": "debian", "raspbian": "debian",
    "devuan": "debian", "deepin": "debian",
    "rhel": "fedora", "centos": "fedora", "rocky": "fedora", "almalinux": "fedora",
    "nobara": "fedora", "bazzite": "fedora", "silverblue": "fedora",
    "manjaro": "arch", "endeavouros": "arch", "garuda": "arch", "cachyos": "arch",
    "arcolinux": "arch", "steamos": "arch",
    "opensuse": "suse", "opensuse-leap": "suse", "opensuse-tumbleweed": "suse",
    "sles": "suse",
    "postmarketos": "alpine",
}


def _os_release_path() -> str:
    """The *host's* os-release, not the Flatpak runtime's."""
    if os.path.exists("/.flatpak-info"):
        for candidate in ("/run/host/os-release", "/run/host/etc/os-release"):
            if os.path.exists(candidate):
                return candidate
    return "/etc/os-release"


@functools.lru_cache(maxsize=1)
def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(_os_release_path(), encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip().strip('"\'')
    except OSError:
        pass
    return values


@functools.lru_cache(maxsize=1)
def family() -> str | None:
    """Which packaging family this system belongs to, or None if unknown."""
    release = _os_release()
    identifier = release.get("ID", "").lower()

    if identifier in _COMMANDS:
        return identifier
    if identifier in _ALIASES:
        return _ALIASES[identifier]

    # ID_LIKE lists parent distributions, most specific first.
    for parent in release.get("ID_LIKE", "").lower().split():
        if parent in _COMMANDS:
            return parent
        if parent in _ALIASES:
            return _ALIASES[parent]
    return None


def name() -> str:
    """A human-readable distribution name, for diagnostics."""
    release = _os_release()
    return release.get("PRETTY_NAME") or release.get("NAME") or "Linux"


def install_command(tool: str) -> str | None:
    """The exact command this user should run to install ``tool``.

    Returns None when we can't tell — better to say nothing than to print a
    command that will fail.
    """
    detected = family()
    if detected is None:
        return None
    package = _PACKAGES.get(tool, {}).get(detected)
    if not package:
        return None
    return _COMMANDS[detected].format(packages=package)


def install_hint(tool: str) -> str | None:
    """A full sentence naming the command, or a graceful fallback."""
    command = install_command(tool)
    if command:
        return f"Install it with:  {command}"
    package = next(iter(_PACKAGES.get(tool, {}).values()), tool)
    return (
        f"Install the “{package}” package using your distribution's "
        "software manager."
    )
