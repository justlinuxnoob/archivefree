"""Thunar custom-action merging.

The interesting case is real: Thunar writes its XML declaration as
``<?xml encoding="UTF-8" version="1.0"?>`` — attributes the wrong way round,
which the XML spec forbids and Python's parser rejects, but Thunar itself reads
without complaint. Every XFCE machine with existing custom actions has one of
these files, so failing to parse it meant the integration silently did nothing
on exactly the desktop it was most needed.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import pytest

from archivefree.integration import filemanagers

#: Trimmed from a real MX Linux uca.xml, declaration reproduced verbatim.
THUNAR_UCA = """<?xml encoding="UTF-8" version="1.0"?>
<actions>
<action>
\t<icon>utilities-terminal</icon>
\t<name>Open Terminal Here</name>
\t<unique-id>1448831432130286-1</unique-id>
\t<command>exo-open --working-directory %f --launch TerminalEmulator</command>
\t<description>Example for a custom action</description>
\t<patterns>*</patterns>
\t<startup-notify/>
\t<directories/>
</action>
<action>
\t<icon>gtk-execute</icon>
\t<name>Run command ...</name>
\t<unique-id>1448831432130286-2</unique-id>
\t<command>bash -c "%f"</command>
\t<description>Run this file</description>
\t<patterns>*</patterns>
\t<other-files/>
</action>
</actions>
"""


@pytest.fixture
def fake_config(tmp_path, monkeypatch):
    """Point the integration at a throwaway config directory."""
    monkeypatch.setattr(filemanagers, "_config_home", lambda: str(tmp_path))
    monkeypatch.setattr(filemanagers, "_data_home", lambda: str(tmp_path / "data"))
    thunar_dir = tmp_path / "Thunar"
    thunar_dir.mkdir()
    return thunar_dir / "uca.xml"


def test_thunars_malformed_declaration_is_accepted(fake_config):
    """This is the bug: Python refuses the file Thunar happily wrote."""
    fake_config.write_text(THUNAR_UCA, encoding="utf-8")

    # Confirm the premise — a plain parse really does fail.
    with pytest.raises(ET.ParseError):
        ET.parse(str(fake_config))

    assert filemanagers.install_thunar(), "install failed on a real Thunar uca.xml"


def test_existing_user_actions_survive(fake_config):
    """Never destroy actions someone spent time creating."""
    fake_config.write_text(THUNAR_UCA, encoding="utf-8")
    assert filemanagers.install_thunar()

    root = filemanagers._read_uca(str(fake_config))
    names = {a.findtext("name") for a in root.findall("action")}
    assert "Open Terminal Here" in names
    assert "Run command ..." in names
    assert "Extract Here with ArchiveFree" in names


def test_a_backup_is_kept(fake_config):
    fake_config.write_text(THUNAR_UCA, encoding="utf-8")
    filemanagers.install_thunar()
    backup = str(fake_config) + ".archivefree-backup"
    assert os.path.exists(backup)
    with open(backup, encoding="utf-8") as fh:
        assert "Open Terminal Here" in fh.read()


def test_the_result_is_still_readable_by_thunar(fake_config):
    """We must write the dialect Thunar expects, not "corrected" XML."""
    fake_config.write_text(THUNAR_UCA, encoding="utf-8")
    filemanagers.install_thunar()

    data = fake_config.read_bytes()
    assert data.startswith(b'<?xml encoding="UTF-8" version="1.0"?>'), \
        "declaration was rewritten into a form Thunar does not produce"
    # And we can still read back what we wrote.
    assert filemanagers._read_uca(str(fake_config)) is not None


def test_installing_twice_does_not_duplicate(fake_config):
    fake_config.write_text(THUNAR_UCA, encoding="utf-8")
    filemanagers.install_thunar()
    filemanagers.install_thunar()

    root = filemanagers._read_uca(str(fake_config))
    ours = [a for a in root.findall("action")
            if (a.findtext("unique-id") or "").startswith("archivefree-")]
    assert len(ours) == len(filemanagers._THUNAR_ACTIONS)


def test_uninstall_removes_only_our_actions(fake_config):
    fake_config.write_text(THUNAR_UCA, encoding="utf-8")
    filemanagers.install_thunar()
    assert filemanagers.uninstall_thunar()

    root = filemanagers._read_uca(str(fake_config))
    names = {a.findtext("name") for a in root.findall("action")}
    assert names == {"Open Terminal Here", "Run command ..."}


def test_works_when_there_is_no_existing_file(fake_config):
    assert not fake_config.exists()
    assert filemanagers.install_thunar()
    root = filemanagers._read_uca(str(fake_config))
    assert len(root.findall("action")) == len(filemanagers._THUNAR_ACTIONS)


def test_declarative_actions_are_written(tmp_path, monkeypatch):
    monkeypatch.setattr(filemanagers, "_data_home", lambda: str(tmp_path))
    monkeypatch.setattr(filemanagers, "_config_home", lambda: str(tmp_path))
    assert filemanagers.install_declarative()
    assert (tmp_path / "nemo" / "actions" /
            "archivefree-extract-here.nemo_action").exists()
    assert (tmp_path / "kio" / "servicemenus" / "archivefree.desktop").exists()
    assert (tmp_path / "file-manager" / "actions" /
            "archivefree-compress.desktop").exists()
    assert filemanagers.uninstall_declarative()
    assert not (tmp_path / "nemo" / "actions" /
                "archivefree-extract-here.nemo_action").exists()


def test_scripts_are_executable(tmp_path, monkeypatch):
    """A Nautilus script that isn't executable simply never appears."""
    monkeypatch.setattr(filemanagers, "_data_home", lambda: str(tmp_path))
    monkeypatch.setattr(filemanagers, "_config_home", lambda: str(tmp_path))
    assert filemanagers.install_scripts()
    script = tmp_path / "nautilus" / "scripts" / "Extract Here"
    assert script.exists()
    assert os.access(script, os.X_OK), "script is not executable"
