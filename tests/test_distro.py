"""Per-distribution install advice.

The "a helper program is missing" error is the only message in the app the user
is expected to act on, so the command it prints has to be right on their system.
An apt command shown to a Fedora user is worse than no advice at all.
"""

from __future__ import annotations

import pytest

from archivefree.core import distro
from archivefree.core.errors import MissingTool

# (os-release contents, expected package manager fragment)
CASES = [
    ('ID=debian\n', "apt install"),
    ('ID=ubuntu\nID_LIKE=debian\n', "apt install"),
    ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "apt install"),
    ('ID=mx\nID_LIKE=debian\n', "apt install"),
    ('ID=fedora\n', "dnf install"),
    ('ID=rhel\nID_LIKE="fedora"\n', "dnf install"),
    ('ID=rocky\nID_LIKE="rhel centos fedora"\n', "dnf install"),
    ('ID=nobara\nID_LIKE=fedora\n', "dnf install"),
    ('ID=arch\n', "pacman -S"),
    ('ID=manjaro\nID_LIKE=arch\n', "pacman -S"),
    ('ID=endeavouros\nID_LIKE=arch\n', "pacman -S"),
    ('ID=steamos\nID_LIKE=arch\n', "pacman -S"),
    ('ID=opensuse-tumbleweed\nID_LIKE="opensuse suse"\n', "zypper install"),
    ('ID=alpine\n', "apk add"),
    ('ID=gentoo\n', "emerge"),
    ('ID=void\n', "xbps-install"),
]


@pytest.fixture(autouse=True)
def clear_caches():
    distro._os_release.cache_clear()
    distro.family.cache_clear()
    yield
    distro._os_release.cache_clear()
    distro.family.cache_clear()


def use_os_release(monkeypatch, tmp_path, contents: str) -> None:
    path = tmp_path / "os-release"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(distro, "_os_release_path", lambda: str(path))
    distro._os_release.cache_clear()
    distro.family.cache_clear()


@pytest.mark.parametrize("contents,expected", CASES)
def test_install_command_matches_the_distribution(contents, expected,
                                                  monkeypatch, tmp_path):
    use_os_release(monkeypatch, tmp_path, contents)
    command = distro.install_command("7z")
    assert command is not None, f"no command produced for:\n{contents}"
    assert expected in command, f"expected {expected!r} in {command!r}"


def test_fedora_gets_the_fedora_package_name(monkeypatch, tmp_path):
    """7-Zip is 'p7zip' on Fedora, not Debian's '7zip'."""
    use_os_release(monkeypatch, tmp_path, "ID=fedora\n")
    command = distro.install_command("7z")
    assert "p7zip" in command
    assert "apt" not in command


def test_debian_gets_the_debian_package_name(monkeypatch, tmp_path):
    use_os_release(monkeypatch, tmp_path, "ID=debian\n")
    assert distro.install_command("7z") == "sudo apt install 7zip"


def test_unknown_distribution_gives_generic_advice(monkeypatch, tmp_path):
    """Never print a command that would fail — say something useful instead."""
    use_os_release(monkeypatch, tmp_path, "ID=some-new-distro\n")
    assert distro.install_command("7z") is None
    hint = distro.install_hint("7z")
    assert hint and "software manager" in hint
    assert "apt" not in hint and "dnf" not in hint


def test_missing_os_release_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(distro, "_os_release_path", lambda: str(tmp_path / "nope"))
    distro._os_release.cache_clear()
    distro.family.cache_clear()
    assert distro.family() is None
    assert distro.install_hint("7z")  # still returns something usable


def test_the_error_shown_to_users_carries_the_right_command(monkeypatch, tmp_path):
    """End to end: the exception a Fedora user sees must not mention apt."""
    use_os_release(monkeypatch, tmp_path, "ID=fedora\n")
    error = MissingTool("7z", "open 7z files", "7zip")
    assert "dnf" in error.hint
    assert "apt" not in error.hint
    # The message itself stays distribution-neutral.
    assert "7z" in error.message


def test_every_family_has_a_package_for_every_tool():
    """A missing entry would silently degrade to generic advice."""
    for tool, per_family in distro._PACKAGES.items():
        for family in distro._COMMANDS:
            assert family in per_family, f"{tool} has no package name for {family}"
