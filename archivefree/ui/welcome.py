"""First-run offer to become the default archive handler, and the settings for it.

This is the app's reason for existing, so it's asked plainly, once, on first
run — with an honest description of what changes and a one-click undo. It never
needs root and it never edits anything outside the user's own config.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from .._version import APP_NAME
from ..config import config
from ..integration import defaults


class FirstRunDialog(Adw.Dialog):
    """Shown once, the first time the app starts."""

    __gtype_name__ = "ArchiveFreeFirstRunDialog"

    def __init__(self, application):
        super().__init__(title=f"Welcome to {APP_NAME}", content_width=520)
        self.application = application
        self.settings = config()

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_title=False))

        page = Adw.PreferencesPage()

        status = Adw.StatusPage(
            icon_name="io.github.justlinuxnoob.ArchiveFree",
            title="Look before you unpack",
            description=(
                f"{APP_NAME} opens archives in a window so you can see what’s "
                "inside and choose what to extract — instead of files being "
                "dumped into the folder the moment you double-click."
            ),
        )
        status.set_margin_bottom(6)
        group = Adw.PreferencesGroup()
        group.add(status)
        page.add(group)

        action_group = Adw.PreferencesGroup(
            title="Make it the default?",
            description=(
                "Double-clicking a zip, 7z, rar or tar file would open it here. "
                "This only changes your own settings, needs no password, and can "
                "be switched back at any time in Preferences."
            ),
        )
        self.result_row = Adw.ActionRow(visible=False)
        action_group.add(self.result_row)
        page.add(action_group)

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=6,
                          margin_bottom=12)
        self.skip_button = Gtk.Button(label="Not Now")
        self.skip_button.add_css_class("pill")
        self.skip_button.connect("clicked", self._on_skip)
        self.accept_button = Gtk.Button(label="Yes, Make It Default")
        self.accept_button.add_css_class("pill")
        self.accept_button.add_css_class("suggested-action")
        self.accept_button.connect("clicked", self._on_accept)
        buttons.append(self.skip_button)
        buttons.append(self.accept_button)

        button_group = Adw.PreferencesGroup()
        button_group.add(buttons)
        page.add(button_group)

        toolbar.set_content(page)
        self.set_child(toolbar)

    def _on_accept(self, *_args) -> None:
        changed, warnings = defaults.set_as_default()
        self.settings["default_handler_offered"] = True
        self.settings["first_run_completed"] = True

        if changed and not warnings:
            self._finish(f"{APP_NAME} now opens archive files.", "emblem-ok-symbolic")
        elif changed:
            self._finish(
                f"{APP_NAME} now opens most archive files. "
                f"{len(warnings)} type(s) couldn’t be changed.",
                "dialog-warning-symbolic",
            )
        else:
            self._finish(
                "Your desktop wouldn’t let that setting change. "
                "You can still set it from your file manager’s “Open With” menu.",
                "dialog-warning-symbolic",
            )

    def _finish(self, message: str, icon: str) -> None:
        self.result_row.set_title(message)
        self.result_row.set_visible(True)
        self.result_row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        self.accept_button.set_visible(False)
        self.skip_button.set_label("Done")
        self.skip_button.remove_css_class("pill")
        self.skip_button.add_css_class("pill")
        self.skip_button.add_css_class("suggested-action")

    def _on_skip(self, *_args) -> None:
        self.settings["default_handler_offered"] = True
        self.settings["first_run_completed"] = True
        self.close()


class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "ArchiveFreePreferencesDialog"

    def __init__(self, application):
        super().__init__()
        self.application = application
        self.settings = config()

        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")

        # -- default handler
        self.handler_group = Adw.PreferencesGroup(
            title="Default Archive Application",
            description="Changes only your own settings. No password needed.",
        )
        self.handler_row = Adw.ActionRow()
        self.handler_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.handler_button.connect("clicked", self._on_toggle_handler)
        self.handler_row.add_suffix(self.handler_button)
        self.handler_group.add(self.handler_row)
        page.add(self.handler_group)
        self._refresh_handler_row()

        # -- file manager menus
        from ..integration import filemanagers

        detected = filemanagers.detected_file_managers()
        self.menus_group = Adw.PreferencesGroup(
            title="File Manager Menus",
            description=(
                "Adds “Extract Here” and “Compress…” to the right-click menu of "
                + (", ".join(detected) if detected else "your file manager")
                + "."
            ),
        )
        self.menus_row = Adw.ActionRow()
        self.menus_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.menus_button.connect("clicked", self._on_toggle_menus)
        self.menus_row.add_suffix(self.menus_button)
        self.menus_group.add(self.menus_row)
        page.add(self.menus_group)
        self._refresh_menus_row()

        # -- extraction
        extract_group = Adw.PreferencesGroup(title="Extracting")
        self.subfolder_row = Adw.SwitchRow(
            title="Extract into a new folder by default",
            subtitle="Stops loose files scattering across the destination",
            active=bool(self.settings["create_subfolder"]),
        )
        self.subfolder_row.connect(
            "notify::active",
            lambda row, _: self.settings.set("create_subfolder", row.get_active()))
        extract_group.add(self.subfolder_row)

        self.open_after_row = Adw.SwitchRow(
            title="Open the folder when extraction finishes",
            active=bool(self.settings["open_folder_after_extract"]),
        )
        self.open_after_row.connect(
            "notify::active",
            lambda row, _: self.settings.set("open_folder_after_extract",
                                             row.get_active()))
        extract_group.add(self.open_after_row)
        page.add(extract_group)

        # -- columns
        columns_group = Adw.PreferencesGroup(title="Contents List")
        for key, title in [("show_compressed_column", "Show the Compressed column"),
                           ("show_type_column", "Show the Type column"),
                           ("show_modified_column", "Show the Modified column")]:
            row = Adw.SwitchRow(title=title, active=bool(self.settings[key]))
            row.connect("notify::active", self._on_column_toggled, key)
            columns_group.add(row)
        page.add(columns_group)

        self.add(page)
        self.add(self._backends_page())

    def _backends_page(self) -> Adw.PreferencesPage:
        """An honest account of which formats work on this machine, and why."""
        from ..core import tools

        page = Adw.PreferencesPage(title="Formats",
                                   icon_name="package-x-generic-symbolic")
        builtin = Adw.PreferencesGroup(
            title="Built In",
            description="These work without any extra software.",
        )
        builtin.add(Adw.ActionRow(
            title="ZIP, TAR, GZ, BZ2, XZ",
            subtitle="Handled directly by ArchiveFree",
        ))
        page.add(builtin)

        helpers = Adw.PreferencesGroup(
            title="Helper Programs",
            description="Install these to support more formats.",
        )
        for name, purpose, present in tools.available_report():
            row = Adw.ActionRow(title=name, subtitle=purpose)
            icon = Gtk.Image.new_from_icon_name(
                "emblem-ok-symbolic" if present else "dialog-warning-symbolic")
            icon.add_css_class("success" if present else "warning")
            icon.set_tooltip_text("Installed" if present else "Not installed")
            row.add_suffix(icon)
            helpers.add(row)
        page.add(helpers)
        return page

    def _refresh_handler_row(self) -> None:
        if not defaults.is_installed():
            self.handler_row.set_title("ArchiveFree isn’t installed system-wide")
            self.handler_row.set_subtitle(
                "Install the .deb or Flatpak to set it as the default handler."
            )
            self.handler_button.set_label("Unavailable")
            self.handler_button.set_sensitive(False)
            return

        handled, total = defaults.status()
        if defaults.is_default():
            self.handler_row.set_title("ArchiveFree opens archive files")
            self.handler_row.set_subtitle(f"Handling {handled} of {total} archive types")
            self.handler_button.set_label("Undo")
            self.handler_button.remove_css_class("suggested-action")
        else:
            self.handler_row.set_title("Another application opens archive files")
            self.handler_row.set_subtitle(
                "Double-clicking an archive won’t open it in ArchiveFree"
            )
            self.handler_button.set_label("Make Default")
            self.handler_button.add_css_class("suggested-action")
        self.handler_button.set_sensitive(True)

    def _on_toggle_handler(self, *_args) -> None:
        if defaults.is_default():
            defaults.unset_as_default()
            self.add_toast(Adw.Toast(title="Restored your previous archive application."))
        else:
            changed, _warnings = defaults.set_as_default()
            if changed:
                self.add_toast(Adw.Toast(title="ArchiveFree now opens archive files."))
            else:
                self.add_toast(Adw.Toast(
                    title="Your desktop wouldn’t let that setting change."))
        self._refresh_handler_row()

    def _refresh_menus_row(self) -> None:
        from ..integration import filemanagers

        if filemanagers.is_installed():
            self.menus_row.set_title("Right-click menu entries are installed")
            self.menus_row.set_subtitle(
                "You may need to restart your file manager to see them"
            )
            self.menus_button.set_label("Remove")
            self.menus_button.remove_css_class("suggested-action")
        else:
            self.menus_row.set_title("Not added yet")
            self.menus_row.set_subtitle(
                "Reach ArchiveFree without opening it first"
            )
            self.menus_button.set_label("Add to Menus")
            self.menus_button.add_css_class("suggested-action")

    def _on_toggle_menus(self, *_args) -> None:
        from ..integration import filemanagers

        if filemanagers.is_installed():
            filemanagers.uninstall_all()
            self.add_toast(Adw.Toast(title="Removed the right-click menu entries."))
        else:
            results = filemanagers.install_all()
            failed = [name for name, ok in results.items() if not ok]
            if failed:
                self.add_toast(Adw.Toast(
                    title=f"Added, except for {failed[0]}."))
            else:
                self.add_toast(Adw.Toast(
                    title="Added. Restart your file manager to see them."))
        self._refresh_menus_row()

    def _on_column_toggled(self, row: Adw.SwitchRow, _param, key: str) -> None:
        self.settings[key] = row.get_active()
        self.application.apply_column_visibility()
