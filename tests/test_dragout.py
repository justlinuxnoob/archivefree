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

pytest.importorskip("gi", reason="PyGObject required for the drag payload module")


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
    assert "text/uri-list" in mimes, f"only offered {mimes}"
