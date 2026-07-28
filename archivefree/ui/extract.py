"""The extract dialog: where to put it, and what shape to put it in."""

from __future__ import annotations

import os

from gi.repository import Adw, Gio, GLib, Gtk

from ..config import config
from ..core import tree
from .utils import format_count, format_size, home_relative


class ExtractDialog(Adw.Dialog):
    """Choose a destination and a couple of options, then extract.

    The default is chosen to be right most of the time: a new folder named
    after the archive, beside the archive — unless the archive already wraps its
    contents in a single folder, in which case adding another would be silly.
    """

    __gtype_name__ = "ArchiveFreeExtractDialog"

    def __init__(self, window, entries=None, selection_label: str | None = None):
        super().__init__(title="Extract", content_width=520)
        self.window = window
        self.entries = entries
        self.settings = config()

        archive_path = window.archive_path or ""
        self._archive_dir = os.path.dirname(archive_path)
        self._suggested_folder = tree.suggested_folder_name(archive_path)
        # An archive whose contents already sit in one folder doesn't need another.
        all_entries = window.backend.list_entries() if window.backend else []
        self._has_own_root = tree.common_root(all_entries) is not None
        self._destination = self.settings["last_extract_dir"] or self._archive_dir \
            or GLib.get_home_dir()

        self._build(entries, selection_label)

    def _build(self, entries, selection_label: str | None) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        self.extract_button = Gtk.Button(label="Extract")
        self.extract_button.add_css_class("suggested-action")
        self.extract_button.connect("clicked", self._on_extract)
        self.extract_button.set_receives_default(True)
        header.pack_end(self.extract_button)
        # Without this, Enter just dismisses the dialog and nothing is extracted.
        self.set_default_widget(self.extract_button)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        # -- what is being extracted
        summary_group = Adw.PreferencesGroup()
        if entries is None:
            count = sum(1 for e in (self.window.backend.list_entries()
                                    if self.window.backend else []) if not e.is_dir)
            total = sum(e.size for e in (self.window.backend.list_entries()
                                         if self.window.backend else []) if not e.is_dir)
            title = "Everything in this archive"
        else:
            files = [e for e in entries if not e.is_dir]
            count = len(files)
            total = sum(e.size for e in files)
            title = selection_label or "Selected items"
        summary = Adw.ActionRow(
            title=title,
            subtitle=f"{format_count(count, 'file')} · {format_size(total)} once unpacked",
        )
        summary.add_prefix(Gtk.Image.new_from_icon_name("package-x-generic-symbolic"))
        summary_group.add(summary)
        page.add(summary_group)

        # -- destination. The description doubles as a live preview of the exact
        # path, kept at the top of the group so it can't scroll out of sight.
        destination_group = Adw.PreferencesGroup(title="Where to put it")
        self.destination_group = destination_group
        self.destination_row = Adw.ActionRow(
            title="Destination folder",
            subtitle=home_relative(self._destination),
            activatable=True,
        )
        self.destination_row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose_folder)
        self.destination_row.add_suffix(choose)
        self.destination_row.connect("activated", self._on_choose_folder)
        destination_group.add(self.destination_row)

        self.subfolder_row = Adw.SwitchRow(
            title="Put it in its own folder",
            subtitle=f"Creates “{self._suggested_folder}” so files don’t scatter",
            active=bool(self.settings["create_subfolder"]) and not self._has_own_root,
        )
        if self._has_own_root:
            self.subfolder_row.set_subtitle(
                "Not needed — this archive already keeps everything in one folder"
            )
        self.subfolder_row.connect("notify::active", lambda *_: self._update_preview())
        destination_group.add(self.subfolder_row)
        page.add(destination_group)

        # -- options
        options_group = Adw.PreferencesGroup(title="Options")
        self.flatten_row = Adw.SwitchRow(
            title="Ignore folder structure",
            subtitle="Put every file directly in the destination, with no subfolders",
            active=False,
        )
        options_group.add(self.flatten_row)

        self.open_after_row = Adw.SwitchRow(
            title="Open the folder when finished",
            active=bool(self.settings["open_folder_after_extract"]),
        )
        options_group.add(self.open_after_row)
        page.add(options_group)

        toolbar.set_content(page)
        self.set_child(toolbar)
        self._update_preview()

    # ------------------------------------------------------------------
    def _final_destination(self) -> str:
        if self.subfolder_row.get_active():
            return os.path.join(self._destination, self._suggested_folder)
        return self._destination

    def _update_preview(self) -> None:
        target = self._final_destination()
        self.destination_group.set_description(f"Files will be written to  {target}")
        self.destination_row.set_subtitle(home_relative(self._destination))

    def _on_choose_folder(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Choose Destination Folder",
                                accept_label="Extract Here")
        if os.path.isdir(self._destination):
            dialog.set_initial_folder(Gio.File.new_for_path(self._destination))

        def done(source, result) -> None:
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self._destination = folder.get_path()
                self._update_preview()

        dialog.select_folder(self.window, None, done)

    def _on_extract(self, *_args) -> None:
        destination = self._final_destination()
        flatten = self.flatten_row.get_active()
        open_after = self.open_after_row.get_active()

        self.settings["create_subfolder"] = self.subfolder_row.get_active()
        self.settings["open_folder_after_extract"] = open_after
        self.settings["last_extract_dir"] = self._destination

        self.close()
        self.window.start_extraction(destination, self.entries, flatten=flatten,
                                     open_after=open_after)
