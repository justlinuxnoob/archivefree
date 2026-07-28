"""The application object: command line handling, windows, global actions."""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ._version import APP_ID, APP_NAME, __version__  # noqa: E402
from .config import config  # noqa: E402
from .core import jobs  # noqa: E402

STYLE = """
/* Contents list ---------------------------------------------------- */
columnview.af-contents > listview > row {
    padding: 2px 0;
    min-height: 34px;
}
columnview.af-contents > listview > row:selected {
    /* Keep the accent readable rather than letting it swamp the row */
    background-image: none;
}
.af-folder-icon { color: @accent_color; }

/* Breadcrumb bar --------------------------------------------------- */
.af-crumb-bar {
    padding: 4px 8px;
    border-bottom: 1px solid @borders;
    background-color: @view_bg_color;
}
button.af-crumb {
    padding: 2px 8px;
    min-height: 26px;
    font-weight: normal;
}
.af-crumb-current {
    font-weight: bold;
    padding: 2px 8px;
}

/* Status bar ------------------------------------------------------- */
.af-statusbar {
    padding: 5px 12px;
    border-top: 1px solid @borders;
}

/* Progress --------------------------------------------------------- */
.af-progress {
    padding: 10px 12px;
    border-top: 1px solid @borders;
    background-color: @view_bg_color;
}
.af-progress-bar { min-height: 6px; }

/* Previews --------------------------------------------------------- */
.af-preview-text { font-size: 0.92em; }
.af-preview-image {
    background-color: @view_bg_color;
    background-image:
        linear-gradient(45deg, alpha(@window_fg_color, 0.05) 25%, transparent 25%),
        linear-gradient(-45deg, alpha(@window_fg_color, 0.05) 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, alpha(@window_fg_color, 0.05) 75%),
        linear-gradient(-45deg, transparent 75%, alpha(@window_fg_color, 0.05) 75%);
    background-size: 20px 20px;
    background-position: 0 0, 0 10px, 10px -10px, -10px 0;
}

/* Copyable install command in error dialogs ------------------------ */
frame.af-command {
    padding: 8px 10px;
    background-color: alpha(@window_fg_color, 0.06);
}
frame.af-command label { font-family: monospace; }
"""


class ArchiveFreeApplication(Adw.Application):
    __gtype_name__ = "ArchiveFreeApplication"

    def __init__(self) -> None:
        # HANDLES_COMMAND_LINE, not HANDLES_OPEN. The difference matters: with
        # HANDLES_OPEN the option flags are parsed in the *launching* process
        # while do_open runs in the *primary* one, so "--new-archive" never
        # reached an app that was already running — the file manager's Compress
        # entry just made it try to open the folder as an archive. Command-line
        # handling runs in the primary instance and receives the whole argv.
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.settings = config()
        self._add_main_option_entries()

    def _add_main_option_entries(self) -> None:
        self.add_main_option("version", ord("v"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Show the version and exit", None)
        self.add_main_option("new-archive", ord("c"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE,
                             "Compress the given files into a new archive", None)
        self.add_main_option("extract-here", ord("x"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE,
                             "Extract the given archive beside itself", None)
        self.add_main_option("extract-to", ord("d"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING,
                             "Extract the given archive into this folder", "FOLDER")
        self.connect("handle-local-options", self._on_local_options)

    def _on_local_options(self, _app, options: GLib.VariantDict) -> int:
        # Only --version is answered locally; everything else has to reach the
        # primary instance, which is what do_command_line is for.
        if options.contains("version"):
            print(f"{APP_NAME} {__version__}")
            return 0
        return -1  # carry on

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        """Handle an invocation, whether or not an instance is already running.

        This always executes in the primary instance, so the flags and the file
        list arrive together no matter which process the user actually started.
        """
        options = command_line.get_options_dict()
        mode_create = options.contains("new-archive")
        mode_extract_here = options.contains("extract-here")
        destination = options.lookup_value("extract-to", GLib.VariantType("s"))
        mode_extract_to = destination.get_string() if destination else None

        # Paths from a file manager may be relative to *its* directory, not ours.
        cwd = command_line.get_cwd() or os.getcwd()
        paths = []
        for argument in command_line.get_arguments()[1:]:
            if argument.startswith("-"):
                continue
            paths.append(argument if os.path.isabs(argument)
                         else os.path.normpath(os.path.join(cwd, argument)))

        self.activate_with(paths, mode_create, mode_extract_here, mode_extract_to)
        return 0

    def activate_with(self, paths: list[str], mode_create: bool,
                      mode_extract_here: bool, mode_extract_to: str | None) -> None:
        if not paths:
            window = self._new_window()
            window.present()
            self._maybe_offer_default(window)
            return

        if mode_create:
            existing = self.get_active_window()
            window = existing or self._new_window()
            window.present()
            # A window opened solely to compress something should close when the
            # job is done — being asked to compress a folder is a one-shot
            # errand, not a request to leave an archive manager running. A
            # window that was already open belongs to the user and stays.
            self.open_create_dialog(paths, parent=window,
                                    close_when_done=existing is None)
            return

        if mode_extract_here or mode_extract_to:
            self._headless_extract(paths, mode_extract_to)
            return

        self.open_paths(paths)

    # ------------------------------------------------------------------
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # From here on, job callbacks must hop onto the GTK main loop.
        jobs.use_main_loop(True)

        provider = Gtk.CssProvider()
        provider.load_from_string(STYLE)
        from gi.repository import Gdk

        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._register_icon_path()
        self._install_actions()

        # Drag-to-extract stages real files from the user's archives. A window
        # cleans up its own, but a crash or a force-quit would leave them
        # behind; single-instance means anything here now is abandoned.
        from .ui.dragout import prune_orphans

        prune_orphans()

    def _register_icon_path(self) -> None:
        """Let a source checkout find its icons without being installed."""
        from gi.repository import Gdk

        data_dir = os.environ.get("ARCHIVEFREE_DATA_DIR")
        if not data_dir:
            return
        icons = os.path.join(data_dir, "icons")
        if os.path.isdir(icons):
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            theme.add_search_path(icons)

    def _install_actions(self) -> None:
        for name, handler, accels in [
            ("create", self._action_create, ["<Control>n"]),
            ("preferences", self._action_preferences, ["<Control>comma"]),
            ("about", self._action_about, None),
            ("set-default", self._action_set_default, None),
            ("quit", lambda *_: self.quit(), ["<Control>q"]),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    # ------------------------------------------------------------------
    def do_activate(self) -> None:
        window = self._new_window()
        window.present()
        self._maybe_offer_default(window)

    def open_paths(self, paths: list[str]) -> None:
        window = None
        for path in paths:
            window = self._new_window(path)
            window.present()
        if window is not None:
            self._maybe_offer_default(window)

    def _new_window(self, path: str | None = None):
        from .ui.window import ArchiveWindow

        window = ArchiveWindow(self, path)
        self.apply_column_visibility(window)
        return window

    def _headless_extract(self, paths: list[str],
                          destination: str | None = None) -> None:
        """Extract straight away, with a window showing progress.

        Used by the file-manager "Extract Here" menu entries: the user gets a
        real progress bar and a cancel button rather than a frozen file manager.
        """
        for path in paths:
            window = self._new_window(path)
            window.present()

            def once(win=window, source=path):
                if win.backend is None:
                    return True  # still loading; check again
                if destination:
                    win.start_extraction(destination, entries=None)
                else:
                    win._action_extract_here()
                return False

            GLib.timeout_add(120, once)

    # ------------------------------------------------------------------
    def open_create_dialog(self, paths: list[str], parent=None,
                           close_when_done: bool = False) -> None:
        from .ui.create import CreateDialog

        window = parent or self.get_active_window()
        if window is None:
            window = self._new_window()
            window.present()
            close_when_done = True
        CreateDialog(self, paths, parent_window=window,
                     close_when_done=close_when_done).present(window)

    def _action_create(self, *_args) -> None:
        self.open_create_dialog([])

    def _action_preferences(self, *_args) -> None:
        from .ui.welcome import PreferencesDialog

        PreferencesDialog(self).present(self.get_active_window())

    def _action_set_default(self, *_args) -> None:
        from .ui.welcome import FirstRunDialog

        FirstRunDialog(self).present(self.get_active_window())

    def _action_about(self, *_args) -> None:
        from .core import tools

        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=__version__,
            developer_name="The ArchiveFree contributors",
            comments=(
                "A free, ad-free archive manager for Linux.\n\n"
                "Look inside an archive before anything is unpacked, then "
                "extract exactly what you want, where you want it."
            ),
            website="https://github.com/justlinuxnoob/archivefree",
            issue_url="https://github.com/justlinuxnoob/archivefree/issues",
            license_type=Gtk.License.GPL_3_0,
            copyright="© 2026 The ArchiveFree contributors",
        )
        about.add_credit_section("Built with", [
            "GTK https://gtk.org",
            "libadwaita https://gnome.pages.gitlab.gnome.org/libadwaita/",
            "7-Zip https://7-zip.org",
        ])
        details = tools.sevenzip_version()
        about.set_debug_info(
            f"{APP_NAME} {__version__}\n"
            f"Python {sys.version.split()[0]}\n"
            f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}."
            f"{Gtk.get_micro_version()}\n"
            f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}."
            f"{Adw.get_micro_version()}\n"
            f"7-Zip: {details or 'not installed'}\n"
        )
        about.present(self.get_active_window())

    # ------------------------------------------------------------------
    def _maybe_offer_default(self, window) -> None:
        """Ask once, on first run, and only if it would actually work."""
        if self.settings["default_handler_offered"]:
            return
        from .integration import defaults

        if not defaults.is_installed() or defaults.is_default():
            # Running from a source checkout, or already the default: don't ask.
            self.settings["default_handler_offered"] = True
            return

        from .ui.welcome import FirstRunDialog

        GLib.timeout_add(400, lambda: FirstRunDialog(self).present(window) or False)

    def apply_column_visibility(self, window=None) -> None:
        windows = [window] if window else self.get_windows()
        for win in windows:
            browser = getattr(win, "browser", None)
            if browser is None:
                continue
            browser.set_column_visible("compressed",
                                       bool(self.settings["show_compressed_column"]))
            browser.set_column_visible("type", bool(self.settings["show_type_column"]))
            browser.set_column_visible("modified",
                                       bool(self.settings["show_modified_column"]))


def main(argv: list[str] | None = None) -> int:
    return ArchiveFreeApplication().run(argv if argv is not None else sys.argv)
