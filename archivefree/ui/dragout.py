"""Dragging files out of an archive to extract them.

This is the feature every other Linux archive manager gets wrong. File Roller,
Engrampa and Xarchiver all implement drag-out with XDND Direct Save (XDS), where
the drop target tells the source where to write the file. Wayland has no
equivalent of XDS, so on every Wayland desktop the drag silently does nothing —
a bug that has been open against File Roller for over five years.

We do it the portable way instead: when the drag starts, extract the selection
into a temporary directory and hand the receiver ordinary ``text/uri-list``
URIs. The file manager then copies them like any other file drag, which works
identically on X11 and Wayland because nothing display-server-specific is
involved.

The cost is that extraction happens before the drop rather than after, so the
work is done even if the user aborts the drag. For the handful of files people
actually drag that is imperceptible; :data:`DRAG_SIZE_LIMIT` guards the case
where someone tries to drag a 4 GB selection, and points them at Extract
instead.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from gi.repository import Gdk, Gio, GLib

from ..core.tree import Node

#: Above this, extracting during the drag would visibly freeze the window, so
#: we decline and suggest the Extract button — which has progress and a cancel.
DRAG_SIZE_LIMIT = 96 * 1024 * 1024

#: And above this many files, even small ones add up to a noticeable stall.
DRAG_COUNT_LIMIT = 500


class DragOutHandler:
    """Attaches drag-to-extract to the contents view of a window."""

    def __init__(self, window):
        self.window = window
        self._staging: list[str] = []

    # ------------------------------------------------------------------
    def prepare(self, node: Node, selection_at_press: list[Node] | None = None):
        """Build the drag payload for ``node``.

        Called by each row's own drag source. If the dragged row was part of the
        selection when the button went down, we drag the whole selection — that
        snapshot matters, because pressing a row collapses the selection to it
        before we get here. Otherwise we drag just the row under the cursor.
        """
        selected = selection_at_press if selection_at_press is not None \
            else self.window.browser.selected_nodes()
        if any(n.path == node.path for n in selected) and len(selected) > 1:
            nodes = selected
        else:
            nodes = [node]

        entries = []
        for chosen in nodes:
            entries.extend(chosen.all_entries())
        files = [e for e in entries if not e.is_dir]
        if not files:
            return None

        total = sum(max(e.size, 0) for e in files)
        if total > DRAG_SIZE_LIMIT or len(files) > DRAG_COUNT_LIMIT:
            self.window.toast(
                "That’s a lot to drag — use the Extract button instead, "
                "so you get progress and a cancel button."
            )
            return None

        try:
            paths = self._extract_for_drag(nodes, entries)
        except Exception as exc:
            from .dialogs import present_error

            present_error(self.window, exc, title="Couldn’t Prepare the Drag")
            return None

        if not paths:
            return None
        return _provider_for(paths)

    # ------------------------------------------------------------------
    def _extract_for_drag(self, nodes: list[Node], entries) -> list[str]:
        """Unpack the selection into a temporary directory.

        Returns the top-level paths to hand to the file manager: dragging a
        folder should drop the folder, not its loose contents.
        """
        backend = self.window.backend
        if backend is None:
            return []

        staging = tempfile.mkdtemp(prefix="archivefree-drag-", dir=_drag_root())
        self._staging.append(staging)

        backend.extract(staging, entries=entries, progress=None, on_conflict=None)

        # Map each selected node back to what landed on disk.
        results: list[str] = []
        for node in nodes:
            candidate = os.path.join(staging, node.path)
            if os.path.lexists(candidate):
                results.append(candidate)
        if not results:
            # Fall back to whatever is at the top of the staging directory.
            results = [os.path.join(staging, name) for name in os.listdir(staging)]
        return results

    def cleanup(self) -> None:
        """Remove every staging directory this window created."""
        for path in self._staging:
            shutil.rmtree(path, ignore_errors=True)
        self._staging.clear()


# -- helpers -------------------------------------------------------------


def _drag_root() -> str:
    """Where staging directories live.

    XDG_RUNTIME_DIR is usually a tmpfs that the session cleans up on logout,
    which is exactly the lifetime we want. Fall back to the system temp
    directory when it isn't set (some minimal sessions).
    """
    runtime = GLib.get_user_runtime_dir()
    if runtime and os.path.isdir(runtime):
        root = os.path.join(runtime, "archivefree")
        try:
            os.makedirs(root, exist_ok=True)
            return root
        except OSError:
            pass
    return tempfile.gettempdir()


def _provider_for(paths: list[str]) -> Gdk.ContentProvider:
    """Offer the dragged files in every form a file manager might ask for.

    ``text/uri-list`` is the universal one and is what makes this work on
    Wayland; ``GdkFileList`` is what GTK 4 apps prefer natively. Providing both
    means Nautilus, Nemo, Thunar, Dolphin and PCManFM all accept the drop.
    """
    gfiles = [Gio.File.new_for_path(p) for p in paths]
    providers: list[Gdk.ContentProvider] = []

    try:
        file_list = Gdk.FileList.new_from_list(gfiles)
        providers.append(Gdk.ContentProvider.new_typed(Gdk.FileList, file_list))
    except (AttributeError, TypeError):
        pass  # older GTK without GdkFileList construction from Python

    uri_text = "\r\n".join(f.get_uri() for f in gfiles) + "\r\n"
    providers.append(
        Gdk.ContentProvider.new_for_bytes(
            "text/uri-list", GLib.Bytes.new(uri_text.encode("utf-8"))
        )
    )
    # Plain text so dropping into a terminal or text editor pastes the paths.
    providers.append(
        Gdk.ContentProvider.new_for_value(
            GLib.Variant("s", "\n".join(f.get_path() or "" for f in gfiles)).unpack()
        )
    )

    if len(providers) == 1:
        return providers[0]
    return Gdk.ContentProvider.new_union(providers)
