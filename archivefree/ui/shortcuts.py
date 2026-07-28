"""Keyboard shortcuts reference."""

from __future__ import annotations

from gi.repository import Adw, Gtk

SHORTCUTS = [
    ("Archives", [
        ("<Control>O", "Open an archive"),
        ("<Control>N", "New archive"),
        ("<Control>E", "Extract (selection, or everything)"),
        ("<Control><Shift>E", "Extract everything…"),
        ("<Control>W", "Close window"),
    ]),
    ("Getting Around", [
        ("<alt>Left", "Go back"),
        ("<alt>Up", "Go to the parent folder"),
        ("<Control>F", "Search inside the archive"),
        ("Escape", "Clear search, or cancel the current operation"),
    ]),
    ("Files", [
        ("<Control>A", "Select everything in this folder"),
        ("space", "Preview the selected file"),
        ("Return", "Open folder, or preview file"),
        ("<Control>Return", "Archive properties"),
    ]),
]


class ShortcutsDialog(Adw.Dialog):
    __gtype_name__ = "ArchiveFreeShortcutsDialog"

    def __init__(self) -> None:
        super().__init__(title="Keyboard Shortcuts", content_width=460,
                         content_height=560)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        page = Adw.PreferencesPage()
        for section, entries in SHORTCUTS:
            group = Adw.PreferencesGroup(title=section)
            for accelerator, description in entries:
                row = Adw.ActionRow(title=description)
                row.add_suffix(_accelerator_label(accelerator))
                group.add(row)
            page.add(group)

        toolbar.set_content(page)
        self.set_child(toolbar)


def _accelerator_label(accelerator: str) -> Gtk.Widget:
    label = Gtk.ShortcutLabel(accelerator=accelerator, valign=Gtk.Align.CENTER)
    return label
