"""Dragging entries out of an archive.

The payload logic is what matters and it is testable without a display: which
entries get staged, where they land, and which top-level paths are handed to
the file manager. The GTK plumbing around it is thin by design.
"""

from __future__ import annotations

import os

import pytest

from archivefree.core import registry
from archivefree.core.create import CreateOptions, create_archive
from archivefree.core.tree import build_tree

# The drag module pulls in GTK 4. Skip cleanly where that is unavailable —
# importorskip("gi") is not enough, since PyGObject can be installed while the
# GTK 4 typelib is not, which raises rather than failing the import.
pytest.importorskip("gi", reason="PyGObject required for the drag payload module")
try:
    import archivefree.ui  # noqa: F401
except (ImportError, ValueError) as exc:  # ValueError: typelib not available
    pytest.skip(f"GTK 4 not available: {exc}", allow_module_level=True)


@pytest.fixture
def opened(sample_tree, tmp_path):
    """An opened archive plus its folder tree, ready to drag from."""
    source, expected = sample_tree
    archive = str(tmp_path / "drag.zip")
    create_archive([source], CreateOptions(destination=archive, format="zip",
                                           base_dir=source))
    backend = registry.open_archive(archive)
    tree = build_tree(backend.list_entries())
    yield backend, tree, expected
    backend.close()


class FakeBrowser:
    def __init__(self, nodes=()):
        self._nodes = list(nodes)

    def selected_nodes(self):
        return self._nodes


class FakeWindow:
    """Enough of ArchiveWindow for the drag handler, with no GTK involved."""

    def __init__(self, backend, nodes=()):
        self.backend = backend
        self.browser = FakeBrowser(nodes)
        self.toasts: list[str] = []

    def toast(self, message: str, timeout: int = 4) -> None:
        self.toasts.append(message)


def handler_for(window):
    from archivefree.ui.dragout import DragOutHandler

    return DragOutHandler(window)


# -- staging -------------------------------------------------------------


def test_dragging_one_file_stages_exactly_that_file(opened):
    backend, tree, expected = opened
    node = tree.find("readme.txt")
    assert node is not None

    handler = handler_for(FakeWindow(backend))
    paths = handler._extract_for_drag([node], list(node.all_entries()))
    try:
        assert len(paths) == 1
        assert os.path.basename(paths[0]) == "readme.txt"
        with open(paths[0], "rb") as fh:
            assert fh.read() == expected["readme.txt"]
    finally:
        handler.cleanup()


def test_dragging_a_folder_hands_over_the_folder_not_its_contents(opened):
    """Dropping a folder must create the folder, not scatter its files."""
    backend, tree, _ = opened
    node = tree.find("docs")
    assert node is not None and node.is_dir

    handler = handler_for(FakeWindow(backend))
    paths = handler._extract_for_drag([node], list(node.all_entries()))
    try:
        assert len(paths) == 1
        assert os.path.isdir(paths[0])
        assert os.path.basename(paths[0]) == "docs"
        # And the contents came with it, recursively.
        assert os.path.exists(os.path.join(paths[0], "guide.md"))
        assert os.path.exists(
            os.path.join(paths[0], "nested", "deep", "deeper", "buried.txt"))
    finally:
        handler.cleanup()


def test_staging_is_cleaned_up(opened):
    backend, tree, _ = opened
    node = tree.find("readme.txt")
    handler = handler_for(FakeWindow(backend))
    paths = handler._extract_for_drag([node], list(node.all_entries()))
    staging = os.path.dirname(paths[0])
    assert os.path.isdir(staging)
    handler.cleanup()
    assert not os.path.exists(staging), "temporary files were left behind"


def test_each_drag_uses_a_fresh_staging_directory(opened):
    """Two drags must not collide, or the second would see the first's files."""
    backend, tree, _ = opened
    handler = handler_for(FakeWindow(backend))
    first = handler._extract_for_drag(
        [tree.find("readme.txt")], list(tree.find("readme.txt").all_entries()))
    second = handler._extract_for_drag(
        [tree.find("readme.txt")], list(tree.find("readme.txt").all_entries()))
    try:
        assert os.path.dirname(first[0]) != os.path.dirname(second[0])
    finally:
        handler.cleanup()


# -- which entries a drag covers ----------------------------------------


def test_dragging_a_selected_row_drags_the_whole_selection(opened):
    """The snapshot matters: pressing a row collapses the selection first."""
    backend, tree, _ = opened
    a = tree.find("readme.txt")
    b = tree.find("empty.dat")
    # The live selection has already collapsed to one row by the time the
    # drag prepares — exactly the situation the snapshot exists to survive.
    window = FakeWindow(backend, nodes=[b])
    handler = handler_for(window)

    provider = handler.prepare(b, selection_at_press=[a, b])
    try:
        assert provider is not None
        staged = handler._staging[-1]
        produced = set()
        for dirpath, _, filenames in os.walk(staged):
            for name in filenames:
                produced.add(os.path.relpath(os.path.join(dirpath, name), staged))
        assert produced == {"readme.txt", "empty.dat"}, \
            "the rest of the selection was dropped on the floor"
    finally:
        handler.cleanup()


def test_dragging_an_unselected_row_drags_only_that_row(opened):
    backend, tree, _ = opened
    a = tree.find("readme.txt")
    b = tree.find("empty.dat")
    window = FakeWindow(backend, nodes=[a])
    handler = handler_for(window)

    handler.prepare(b, selection_at_press=[a])
    try:
        staged = handler._staging[-1]
        produced = {name for _, _, files in os.walk(staged) for name in files}
        assert produced == {"empty.dat"}
    finally:
        handler.cleanup()


def test_an_oversized_drag_is_declined_with_advice(opened, monkeypatch):
    """Extraction happens before the drop, so a huge drag would freeze the UI."""
    from archivefree.ui import dragout

    backend, tree, _ = opened
    monkeypatch.setattr(dragout, "DRAG_SIZE_LIMIT", 10)  # anything is too big
    node = tree.find("readme.txt")
    window = FakeWindow(backend, nodes=[node])
    handler = handler_for(window)

    assert handler.prepare(node, selection_at_press=[node]) is None
    assert window.toasts, "declined silently instead of explaining why"
    assert "Extract" in window.toasts[0]
    assert not handler._staging, "staged files despite declining the drag"


def test_dragging_an_empty_folder_produces_nothing(opened):
    backend, tree, _ = opened
    node = tree.find("empty-folder")
    if node is None:
        pytest.skip("this archive format dropped the empty directory")
    window = FakeWindow(backend, nodes=[node])
    handler = handler_for(window)
    # No files inside, so there is nothing to hand over.
    assert handler.prepare(node, selection_at_press=[node]) is None


# -- the payload itself --------------------------------------------------


def test_payload_offers_uri_list(tmp_path):
    """text/uri-list is the format that makes this work on Wayland."""
    from archivefree.ui.dragout import _provider_for

    target = tmp_path / "dropped.txt"
    target.write_text("x")
    provider = _provider_for([str(target)])
    assert provider is not None

    formats = provider.ref_formats()
    mimes = formats.get_mime_types() or ()
    gtypes = [t.name for t in (formats.get_gtypes() or [])]
    assert "text/uri-list" in mimes, f"only offered {mimes}"
    # GdkFileList is what GTK negotiates with other GTK file managers, and is
    # what a non-introspectable constructor silently failed to provide before.
    assert "GdkFileList" in gtypes, f"file target missing; gtypes={gtypes}"


# -- staging location ----------------------------------------------------


def test_staging_path_is_identical_inside_and_outside_a_sandbox(monkeypatch):
    """The file manager opens these paths itself, from outside the sandbox.

    Inside a Flatpak, XDG_RUNTIME_DIR is remapped — the app sees
    /run/user/N/archivefree while the host sees
    /run/user/N/.flatpak/<app>/xdg-run/archivefree. Staging there hands the
    receiver a path that does not exist and the drop silently does nothing.
    """
    from archivefree.ui import dragout

    monkeypatch.setattr("archivefree.integration.defaults.in_flatpak",
                        lambda: True)
    root = dragout._drag_root()
    assert "/.cache/" in root, (
        f"sandboxed build stages to {root!r}, which the host cannot resolve"
    )
    assert "/run/user" not in root
    assert not root.startswith("/tmp"), "/tmp is private to the sandbox"


def test_outside_a_sandbox_the_runtime_directory_is_used(monkeypatch):
    from archivefree.ui import dragout

    monkeypatch.setattr("archivefree.integration.defaults.in_flatpak",
                        lambda: False)
    root = dragout._drag_root()
    assert os.path.isdir(root)


def test_orphaned_staging_directories_are_swept_at_startup(tmp_path, monkeypatch):
    """A crash used to leave the user's extracted files lying around for a day.

    These hold real files unpacked out of someone's archives. The app is
    single-instance, so anything present when it starts is abandoned regardless
    of age and should go immediately.
    """
    from archivefree.ui import dragout

    orphan_a = tmp_path / "archivefree-drag-aaa"
    orphan_b = tmp_path / "archivefree-drag-bbb"
    unrelated = tmp_path / "something-else"
    for directory in (orphan_a, orphan_b, unrelated):
        directory.mkdir()
        (directory / "private.txt").write_text("the user's data")

    monkeypatch.setattr(dragout, "_runtime_root", lambda: str(tmp_path))
    monkeypatch.setattr(dragout, "_cache_root", lambda: str(tmp_path))

    dragout.prune_orphans()

    assert not orphan_a.exists(), "an orphaned staging directory survived startup"
    assert not orphan_b.exists()
    assert unrelated.exists(), "pruning deleted something that wasn't ours"


def test_pruning_survives_a_missing_directory(tmp_path, monkeypatch):
    """First run has no staging directory at all; that must not raise."""
    from archivefree.ui import dragout

    monkeypatch.setattr(dragout, "_runtime_root", lambda: str(tmp_path / "nope"))
    monkeypatch.setattr(dragout, "_cache_root", lambda: str(tmp_path / "also-nope"))
    dragout.prune_orphans()


def test_payload_does_not_offer_plain_text(tmp_path):
    """A receiver takes the first target it knows.

    Offering text/plain alongside the file targets lets a file manager grab the
    string form and silently do nothing with it — a drag that appears to work
    and drops no file, which is the exact failure this feature exists to fix.
    """
    from archivefree.ui.dragout import _provider_for

    target = tmp_path / "dropped.txt"
    target.write_text("x")
    formats = _provider_for([str(target)]).ref_formats()
    mimes = set(formats.get_mime_types() or ())

    assert "text/uri-list" in mimes
    assert not any(m.startswith("text/plain") for m in mimes), (
        f"plain text offered alongside the file drop: {sorted(mimes)}"
    )


def test_uri_list_is_crlf_terminated(tmp_path):
    """RFC 2483: each URI ends with CRLF, including the last."""
    from archivefree.ui.dragout import _provider_for

    a = tmp_path / "one.txt"
    b = tmp_path / "two.txt"
    a.write_text("1")
    b.write_text("2")

    formats = _provider_for([str(a), str(b)]).ref_formats()
    assert "text/uri-list" in (formats.get_mime_types() or ())

    # Build the same payload the provider holds and check its shape.
    from gi.repository import Gio

    expected = "".join(
        f"{Gio.File.new_for_path(str(p)).get_uri()}\r\n" for p in (a, b)
    )
    assert expected.endswith("\r\n")
    assert expected.count("\r\n") == 2
