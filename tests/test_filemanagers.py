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


# -- python extension (top-level menu entries) ---------------------------


def test_extension_source_is_shipped():
    """The installer must be able to find the extension it copies."""
    source = filemanagers._extension_source()
    assert source is not None, "the extension file was not found in any known location"
    assert os.path.exists(source)


def test_extension_is_valid_python():
    """It runs inside the file manager, so a syntax error would break Nautilus."""
    import ast

    source = filemanagers._extension_source()
    with open(source, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert "ArchiveFreeMenuProvider" in classes

    methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    # These are the two entry points every host calls.
    assert "get_file_items" in methods
    assert "get_background_items" in methods


def test_extension_installs_where_the_host_can_load_it(tmp_path, monkeypatch):
    monkeypatch.setattr(filemanagers, "_data_home", lambda: str(tmp_path))
    monkeypatch.setattr(filemanagers, "extension_host_available", lambda _m: True)

    assert filemanagers.install_extension()
    for parent, child in filemanagers._EXTENSION_DIRS.values():
        assert (tmp_path / parent / child / "archivefree.py").exists()

    assert filemanagers.uninstall_extension()
    for parent, child in filemanagers._EXTENSION_DIRS.values():
        assert not (tmp_path / parent / child / "archivefree.py").exists()


def test_extension_is_skipped_when_no_host_can_load_it(tmp_path, monkeypatch):
    """Installing it where no *-python binding exists would be a silent no-op."""
    monkeypatch.setattr(filemanagers, "_data_home", lambda: str(tmp_path))
    monkeypatch.setattr(filemanagers, "extension_host_available", lambda _m: False)

    assert filemanagers.install_extension() is False
    assert not list(tmp_path.rglob("archivefree.py"))


def test_scripts_are_the_fallback_when_no_extension_host(tmp_path, monkeypatch):
    """Without an extension host the user still gets entries, via scripts."""
    monkeypatch.setattr(filemanagers, "_data_home", lambda: str(tmp_path))
    monkeypatch.setattr(filemanagers, "_config_home", lambda: str(tmp_path))
    monkeypatch.setattr(filemanagers, "extension_host_available", lambda _m: False)

    results = filemanagers.install_all()
    assert "Nautilus and Caja scripts" in results
    assert (tmp_path / "nautilus" / "scripts" / "Extract Here").exists()


def test_extension_replaces_scripts_when_available(tmp_path, monkeypatch):
    """Both at once would give the user duplicate entries."""
    monkeypatch.setattr(filemanagers, "_data_home", lambda: str(tmp_path))
    monkeypatch.setattr(filemanagers, "_config_home", lambda: str(tmp_path))

    # Start with scripts installed, as an older version would have left them.
    monkeypatch.setattr(filemanagers, "extension_host_available", lambda _m: False)
    filemanagers.install_all()
    assert (tmp_path / "nautilus" / "scripts" / "Extract Here").exists()

    # Now the host is present: the extension wins and the scripts are removed.
    monkeypatch.setattr(filemanagers, "extension_host_available", lambda _m: True)
    results = filemanagers.install_all()
    assert results.get("Nautilus and Caja menus") is True
    assert (tmp_path / "nautilus-python" / "extensions" / "archivefree.py").exists()
    assert not (tmp_path / "nautilus" / "scripts" / "Extract Here").exists(), \
        "scripts left behind alongside the extension — duplicate menu entries"
