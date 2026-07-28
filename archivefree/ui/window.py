"""The main archive window."""

from __future__ import annotations

import os

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .._version import APP_NAME
from ..config import config
from ..core import detect, registry, tree
from ..core.backends.base import Backend
from ..core.entry import ArchiveInfo
from ..core.errors import ArchiveError, PasswordRequired, WrongPassword
from ..core.jobs import Job, Progress
from ..core.tree import Node
from .browser import ArchiveBrowser
from .dialogs import DialogConflictResolver, ask_password, present_error
from .progress import ProgressBar
from .utils import format_count, format_size, home_relative


class ArchiveWindow(Adw.ApplicationWindow):
    """Shows one archive. Opening another archive opens another window."""

    __gtype_name__ = "ArchiveFreeWindow"

    def __init__(self, application: Adw.Application, path: str | None = None):
        super().__init__(application=application)
        self.settings = config()
        self.backend: Backend | None = None
        self.info: ArchiveInfo | None = None
        self.root: Node | None = None
        self.archive_path: str | None = None
        self._password: str | None = None
        self._active_job: Job | None = None
        self._resolver: DialogConflictResolver | None = None

        self.set_default_size(self.settings["window_width"],
                              self.settings["window_height"])
        if self.settings["window_maximized"]:
            self.maximize()
        self.set_title(APP_NAME)
        self.set_icon_name("io.github.justlinuxnoob.ArchiveFree")

        self._build()
        self._install_actions()
        self._setup_drop_target()

        if path:
            self.open_archive(path)
        else:
            self.stack.set_visible_child_name("welcome")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        toolbar_view = Adw.ToolbarView()

        self.header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=APP_NAME, subtitle="")
        self.header.set_title_widget(self.window_title)

        # -- left side: open, and folder navigation
        open_button = Gtk.Button(icon_name="document-open-symbolic",
                                 tooltip_text="Open an archive (Ctrl+O)")
        open_button.set_action_name("win.open")
        self.header.pack_start(open_button)

        self.nav_box = Gtk.Box(spacing=0)
        self.nav_box.add_css_class("linked")
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic",
                                      tooltip_text="Go back (Alt+Left)")
        self.back_button.set_action_name("win.go-back")
        self.up_button = Gtk.Button(icon_name="go-up-symbolic",
                                    tooltip_text="Go to parent folder (Alt+Up)")
        self.up_button.set_action_name("win.go-up")
        self.nav_box.append(self.back_button)
        self.nav_box.append(self.up_button)
        self.nav_box.set_visible(False)
        self.header.pack_start(self.nav_box)

        # -- right side: extract, search, menu
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                     tooltip_text="Main menu",
                                     menu_model=self._build_menu())
        self.header.pack_end(menu_button)

        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                              tooltip_text="Search this archive (Ctrl+F)")
        self.search_button.set_visible(False)
        self.header.pack_end(self.search_button)

        self.extract_button = Gtk.Button(label="Extract")
        self.extract_button.add_css_class("suggested-action")
        self.extract_button.set_tooltip_text("Extract files (Ctrl+E)")
        self.extract_button.set_action_name("win.extract")
        self.extract_button.set_visible(False)
        self.header.pack_end(self.extract_button)

        toolbar_view.add_top_bar(self.header)

        # -- search bar
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search inside this archive")
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_bar = Gtk.SearchBar(show_close_button=False)
        self.search_bar.set_child(self.search_entry)
        self.search_bar.connect_entry(self.search_entry)
        self.search_button.bind_property(
            "active", self.search_bar, "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        toolbar_view.add_top_bar(self.search_bar)

        # -- breadcrumbs
        self.breadcrumbs = Gtk.Box(spacing=2)
        self.breadcrumbs.add_css_class("af-breadcrumbs")
        crumb_scroller = Gtk.ScrolledWindow(
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
            propagate_natural_width=True,
        )
        crumb_scroller.set_child(self.breadcrumbs)
        self.crumb_bar = Gtk.Box()
        self.crumb_bar.add_css_class("af-crumb-bar")
        self.crumb_bar.append(crumb_scroller)
        self.crumb_bar.set_visible(False)
        toolbar_view.add_top_bar(self.crumb_bar)

        # -- notices (encrypted, damaged, split)
        self.banner = Adw.Banner(revealed=False)
        toolbar_view.add_top_bar(self.banner)

        # -- content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=120)
        self.stack.add_named(self._build_welcome(), "welcome")
        self.stack.add_named(self._build_loading(), "loading")
        # Drag entries straight out to a file manager or the desktop.
        from .dragout import DragOutHandler

        self.drag_out = DragOutHandler(self)
        self.browser = ArchiveBrowser(on_activate=self._on_node_activated,
                                      on_selection_changed=self._update_status,
                                      on_drag_prepare=self.drag_out.prepare)
        self.stack.add_named(self.browser, "browser")
        self.stack.add_named(self._build_empty(), "empty")
        toolbar_view.set_content(self.stack)

        # -- bottom: status + progress
        self.status_bar = Gtk.Box(spacing=12)
        self.status_bar.add_css_class("af-statusbar")
        self.status_label = Gtk.Label(xalign=0.0, hexpand=True, ellipsize=3)
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        self.ratio_label = Gtk.Label(xalign=1.0)
        self.ratio_label.add_css_class("dim-label")
        self.ratio_label.add_css_class("caption")
        self.status_bar.append(self.status_label)
        self.status_bar.append(self.ratio_label)
        self.status_bar.set_visible(False)
        toolbar_view.add_bottom_bar(self.status_bar)

        self.progress = ProgressBar(on_cancel=self._cancel_job)
        toolbar_view.add_bottom_bar(self.progress)

        self.toast_overlay.set_child(toolbar_view)
        self.set_content(self.toast_overlay)

        self.connect("close-request", self._on_close)

    def _build_menu(self) -> Gio.Menu:
        menu = Gio.Menu()

        archive_section = Gio.Menu()
        archive_section.append("Extract All…", "win.extract-all")
        archive_section.append("Extract Selected…", "win.extract-selected")
        archive_section.append("Extract Here", "win.extract-here")
        menu.append_section(None, archive_section)

        tools_section = Gio.Menu()
        tools_section.append("New Archive…", "app.create")
        tools_section.append("Check Integrity", "win.test")
        tools_section.append("Archive Properties", "win.properties")
        menu.append_section(None, tools_section)

        app_section = Gio.Menu()
        app_section.append("Make Default Archive App…", "app.set-default")
        app_section.append("Preferences", "app.preferences")
        app_section.append("Keyboard Shortcuts", "win.shortcuts")
        app_section.append(f"About {APP_NAME}", "app.about")
        menu.append_section(None, app_section)
        return menu

    def _build_welcome(self) -> Gtk.Widget:
        status = Adw.StatusPage(
            icon_name="io.github.justlinuxnoob.ArchiveFree",
            title="Open an Archive",
            description=(
                "Look inside a zip, 7z, rar or tar file before anything is unpacked.\n"
                "Drop one here, or choose a file to get started."
            ),
        )
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                      halign=Gtk.Align.CENTER)
        open_button = Gtk.Button(label="Open Archive…")
        open_button.add_css_class("suggested-action")
        open_button.add_css_class("pill")
        open_button.set_action_name("win.open")
        create_button = Gtk.Button(label="New Archive…")
        create_button.add_css_class("pill")
        create_button.set_action_name("app.create")
        box.append(open_button)
        box.append(create_button)
        status.set_child(box)
        return status

    def _build_loading(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        self.loading_label = Gtk.Label(label="Reading archive…")
        self.loading_label.add_css_class("title-4")
        self.loading_detail = Gtk.Label(label="")
        self.loading_detail.add_css_class("dim-label")
        box.append(spinner)
        box.append(self.loading_label)
        box.append(self.loading_detail)
        return box

    def _build_empty(self) -> Gtk.Widget:
        self.empty_page = Adw.StatusPage(
            icon_name="folder-open-symbolic",
            title="Nothing Here",
            description="This folder is empty.",
        )
        return self.empty_page

    def _setup_drop_target(self) -> None:
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)

        def on_drop(_target, value, _x, _y) -> bool:
            files = value.get_files() if value else []
            if not files:
                return False
            paths = [f.get_path() for f in files if f.get_path()]
            if not paths:
                return False
            if len(paths) == 1 and detect.detect_format(paths[0]):
                self.open_archive(paths[0])
            else:
                # Several files, or something that isn't an archive: the user
                # almost certainly wants to compress them.
                self.get_application().open_create_dialog(paths, parent=self)
            return True

        drop.connect("drop", on_drop)
        self.add_controller(drop)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _install_actions(self) -> None:
        specs = [
            ("open", self._action_open, ["<Control>o"]),
            ("extract", self._action_extract, ["<Control>e"]),
            ("extract-all", self._action_extract_all, ["<Control><Shift>e"]),
            ("extract-selected", self._action_extract_selected, None),
            ("extract-here", self._action_extract_here, None),
            ("go-back", self._action_go_back, ["<alt>Left"]),
            ("go-up", self._action_go_up, ["<alt>Up"]),
            ("select-all", self._action_select_all, ["<Control>a"]),
            ("search", self._action_search, ["<Control>f"]),
            ("preview", self._action_preview, ["space"]),
            ("test", self._action_test, None),
            ("properties", self._action_properties, ["<Control>Return"]),
            ("shortcuts", self._action_shortcuts, ["<Control>question"]),
            ("close", lambda *_: self.close(), ["<Control>w"]),
            ("cancel", lambda *_: self._cancel_job(), ["Escape"]),
        ]
        app = self.get_application()
        for name, handler, accels in specs:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            if accels and app:
                app.set_accels_for_action(f"win.{name}", accels)

    def _action_open(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Open Archive")
        dialog.set_filters(_archive_filters())
        last = self.settings["last_extract_dir"] or GLib.get_home_dir()
        if os.path.isdir(last):
            dialog.set_initial_folder(Gio.File.new_for_path(last))

        def done(source, result) -> None:
            try:
                file = source.open_finish(result)
            except GLib.Error:
                return  # cancelled
            if file and file.get_path():
                self.open_archive(file.get_path())

        dialog.open(self, None, done)

    def _action_extract(self, *_args) -> None:
        """The header button: extracts the selection, or everything if none."""
        if self.browser.selected_nodes():
            self._action_extract_selected()
        else:
            self._action_extract_all()

    def _action_extract_all(self, *_args) -> None:
        if self.backend is None:
            return
        from .extract import ExtractDialog

        ExtractDialog(self, entries=None).present(self)

    def _action_extract_selected(self, *_args) -> None:
        if self.backend is None:
            return
        nodes = self.browser.selected_nodes()
        if not nodes:
            self.toast("Select some files first, then choose Extract Selected.")
            return
        entries = []
        for node in nodes:
            entries.extend(node.all_entries())
        from .extract import ExtractDialog

        ExtractDialog(self, entries=entries, selection_label=_describe(nodes)).present(self)

    def _action_extract_here(self, *_args) -> None:
        if self.backend is None or self.archive_path is None:
            return
        destination = os.path.dirname(self.archive_path)
        folder = tree.suggested_folder_name(self.archive_path)
        if self.settings["create_subfolder"] and tree.common_root(
            self.backend.list_entries()
        ) is None:
            destination = os.path.join(destination, folder)
        self.start_extraction(destination, entries=None)

    def _action_go_back(self, *_args) -> None:
        if self.browser.searching:
            self.search_button.set_active(False)
            return
        self._action_go_up()

    def _action_go_up(self, *_args) -> None:
        if self.browser.navigate_up():
            self._refresh_view()

    def _action_select_all(self, *_args) -> None:
        self.browser.select_all()

    def _action_search(self, *_args) -> None:
        if self.backend is None:
            return
        self.search_button.set_active(not self.search_button.get_active())
        if self.search_button.get_active():
            self.search_entry.grab_focus()

    def _action_preview(self, *_args) -> None:
        nodes = self.browser.selected_nodes()
        if len(nodes) != 1 or nodes[0].is_dir or self.backend is None:
            return
        from .preview import PreviewDialog

        PreviewDialog(self, nodes[0]).present(self)

    def _action_test(self, *_args) -> None:
        if self.backend is None:
            return
        self.progress.begin("Checking archive integrity…")

        def work(progress: Progress):
            return self.backend.test(progress=progress)

        def done(problems: list[str]) -> None:
            self.progress.end()
            if not problems:
                self.toast("This archive passed its integrity check.")
                return
            error = ArchiveError(
                f"{format_count(len(problems), 'problem')} found in this archive.",
                detail="\n".join(problems[:50]),
                hint="Damaged files usually mean an incomplete download.",
            )
            present_error(self, error, title="Integrity Check Failed")

        self._run_job(work, done)

    def _action_properties(self, *_args) -> None:
        if self.info is None:
            return
        from .properties import PropertiesDialog

        PropertiesDialog(self.info, self.backend).present(self)

    def _action_shortcuts(self, *_args) -> None:
        from .shortcuts import ShortcutsDialog

        ShortcutsDialog().present(self)

    # ------------------------------------------------------------------
    # Opening an archive
    # ------------------------------------------------------------------
    def open_archive(self, path: str, password: str | None = None) -> None:
        path = os.path.abspath(path)
        self.archive_path = path
        self._password = password
        self.stack.set_visible_child_name("loading")
        self.loading_label.set_text(f"Reading {os.path.basename(path)}…")
        self.loading_detail.set_text("")
        self.set_title(f"{os.path.basename(path)} — {APP_NAME}")
        self.window_title.set_title(os.path.basename(path))
        self.window_title.set_subtitle("Reading…")
        self.banner.set_revealed(False)

        def work(progress: Progress):
            backend = registry.open_archive(path, password=password)
            entries = backend.list_entries(progress=progress)
            info = backend.info()
            return backend, entries, info

        def done(result) -> None:
            backend, entries, info = result
            self._present_archive(backend, entries, info)

        def failed(exc: BaseException) -> None:
            if isinstance(exc, (PasswordRequired, WrongPassword)):
                self._prompt_password(path, retry=isinstance(exc, WrongPassword))
                return
            self.stack.set_visible_child_name("welcome")
            self.window_title.set_title(APP_NAME)
            self.window_title.set_subtitle("")
            self.set_title(APP_NAME)
            present_error(self, exc, title="Can’t Open This Archive")

        self._run_job(work, done, failed, show_progress=False)

    def _prompt_password(self, path: str, retry: bool) -> None:
        def got(password: str | None) -> None:
            if password is None:
                self.stack.set_visible_child_name("welcome")
                self.window_title.set_title(APP_NAME)
                self.window_title.set_subtitle("")
                return
            self.open_archive(path, password=password)

        ask_password(self, os.path.basename(path), got, retry=retry)

    def _present_archive(self, backend: Backend, entries, info: ArchiveInfo) -> None:
        if self.backend is not None and self.backend is not backend:
            self.backend.close()
        self.backend = backend
        self.info = info
        self.root = tree.build_tree(entries)
        self.browser.load(self.root)

        self.window_title.set_title(os.path.basename(info.path))
        self.window_title.set_subtitle(
            f"{format_count(info.entry_count, 'file')} · {format_size(info.total_size)}"
        )
        self.extract_button.set_visible(True)
        self.search_button.set_visible(True)
        self.nav_box.set_visible(True)
        self.crumb_bar.set_visible(True)
        self.status_bar.set_visible(True)
        self._refresh_view()
        self._show_notices(info, entries)

    def _show_notices(self, info: ArchiveInfo, entries) -> None:
        """Surface anything the user should know before they extract."""
        if info.volumes:
            missing = detect.missing_volumes(info.volumes)
            if missing:
                self.banner.set_title(
                    f"This archive is split into parts and {', '.join(missing)} "
                    "is missing. Extraction will fail partway through."
                )
                self.banner.add_css_class("error")
            else:
                self.banner.set_title(
                    f"Split archive — all {len(info.volumes)} parts found."
                )
            self.banner.set_revealed(True)
        elif info.encrypted and not self._password:
            self.banner.set_title(
                "Some files in this archive are password-protected."
            )
            self.banner.set_revealed(True)
        elif not entries:
            self.banner.set_title("This archive is empty.")
            self.banner.set_revealed(True)

    # ------------------------------------------------------------------
    # View state
    # ------------------------------------------------------------------
    def _on_node_activated(self, node: Node) -> None:
        if node.is_dir:
            if self.browser.searching:
                self.search_button.set_active(False)
            self.browser.navigate_to(node)
            self._refresh_view()
        else:
            from .preview import PreviewDialog

            PreviewDialog(self, node).present(self)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        text = entry.get_text()
        count = self.browser.search(text)
        if count < 0:
            self._refresh_view()
            return
        self._rebuild_breadcrumbs()
        if count == 0:
            self.empty_page.set_title("No Matches")
            self.empty_page.set_description(f"Nothing in this archive matches “{text}”.")
            self.empty_page.set_icon_name("system-search-symbolic")
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("browser")
        self.status_label.set_text(f"{format_count(count, 'match', 'matches')} for “{text}”")

    def _refresh_view(self) -> None:
        current = self.browser.current
        if current is None:
            return
        if self.browser.row_count == 0:
            self.empty_page.set_title("Empty Folder")
            self.empty_page.set_description("There’s nothing inside this folder.")
            self.empty_page.set_icon_name("folder-open-symbolic")
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("browser")
        self._rebuild_breadcrumbs()
        self.up_button.set_sensitive(current.parent is not None)
        self.back_button.set_sensitive(current.parent is not None)
        self._update_status()

    def _rebuild_breadcrumbs(self) -> None:
        child = self.breadcrumbs.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.breadcrumbs.remove(child)
            child = nxt

        if self.browser.searching:
            label = Gtk.Label(label="Search results")
            label.add_css_class("dim-label")
            label.set_margin_start(6)
            self.breadcrumbs.append(label)
            return

        current = self.browser.current
        if current is None:
            return

        chain: list[Node] = []
        node: Node | None = current
        while node is not None:
            chain.append(node)
            node = node.parent
        chain.reverse()

        for index, node in enumerate(chain):
            if index > 0:
                separator = Gtk.Label(label="›")
                separator.add_css_class("dim-label")
                self.breadcrumbs.append(separator)
            text = node.name or (
                os.path.basename(self.archive_path) if self.archive_path else "Archive"
            )
            if index == len(chain) - 1:
                # The folder you're in is a label, not a button: a disabled
                # button reads as "broken", and this one just isn't clickable.
                current = Gtk.Label(label=text)
                current.add_css_class("af-crumb-current")
                self.breadcrumbs.append(current)
            else:
                button = Gtk.Button(label=text)
                button.add_css_class("flat")
                button.add_css_class("af-crumb")
                button.connect("clicked", self._on_crumb_clicked, node)
                self.breadcrumbs.append(button)

    def _on_crumb_clicked(self, _button: Gtk.Button, node: Node) -> None:
        self.browser.navigate_to(node)
        self._refresh_view()

    def _update_status(self) -> None:
        if self.info is None:
            return
        selected = self.browser.selected_nodes()
        if selected:
            files = sum(n.file_count for n in selected)
            total = sum(n.size for n in selected)
            self.status_label.set_text(
                f"{format_count(len(selected), 'item')} selected · "
                f"{format_count(files, 'file')}, {format_size(total)}"
            )
            self.extract_button.set_label("Extract Selected")
        else:
            current = self.browser.current
            if current is not None and not self.browser.searching:
                self.status_label.set_text(
                    f"{format_count(self.browser.row_count, 'item')} in this folder"
                )
            self.extract_button.set_label("Extract")

        if self.info.archive_size and self.info.total_size:
            saved = self.info.ratio
            self.ratio_label.set_text(
                f"{format_size(self.info.archive_size)} on disk"
                + (f" · {saved:.0%} smaller" if saved > 0.01 else "")
            )

    # ------------------------------------------------------------------
    # Running work
    # ------------------------------------------------------------------
    def _run_job(self, work, on_done, on_error=None, show_progress: bool = True) -> None:
        def failed(exc: BaseException) -> None:
            self.progress.end()
            self._active_job = None
            if on_error:
                on_error(exc)
            else:
                present_error(self, exc)

        def finished(result) -> None:
            self._active_job = None
            on_done(result)

        def cancelled() -> None:
            self.progress.end()
            self._active_job = None
            self.toast("Cancelled.")

        job = Job(
            work,
            on_done=finished,
            on_error=failed,
            on_cancelled=cancelled,
            on_progress=self.progress.update if show_progress else None,
        )
        self._active_job = job
        job.start()

    def _cancel_job(self) -> None:
        if self._active_job is not None:
            self.progress.set_cancelling()
            self._active_job.cancel()
        if self._resolver is not None:
            self._resolver.abort()

    def start_extraction(self, destination: str, entries, flatten: bool = False,
                         open_after: bool | None = None) -> None:
        """Extract into ``destination``, reporting progress in the bottom bar."""
        if self.backend is None:
            return
        backend = self.backend
        resolver = DialogConflictResolver(self)
        self._resolver = resolver
        if open_after is None:
            open_after = bool(self.settings["open_folder_after_extract"])

        self.progress.begin(f"Extracting to {home_relative(destination)}")

        def work(progress: Progress):
            return backend.extract(
                destination, entries=entries, progress=progress,
                on_conflict=resolver.resolve, flatten=flatten,
            )

        def done(written: list[str]) -> None:
            self.progress.end()
            self._resolver = None
            self.settings["last_extract_dir"] = os.path.dirname(destination)
            if not written:
                self.toast("Nothing was extracted — every file was skipped.")
                return
            toast = Adw.Toast(
                title=f"Extracted {format_count(len(written), 'file')} to "
                      f"{os.path.basename(destination) or destination}",
                timeout=6,
            )
            toast.set_button_label("Open Folder")
            toast.connect("button-clicked", lambda *_: _open_folder(destination))
            self.toast_overlay.add_toast(toast)
            if open_after:
                _open_folder(destination)

        def failed(exc: BaseException) -> None:
            self.progress.end()
            self._resolver = None
            present_error(self, exc, title="Extraction Failed")

        self._run_job(work, done, failed)

    def toast(self, message: str, timeout: int = 4) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=timeout))

    # ------------------------------------------------------------------
    def _on_close(self, *_args) -> bool:
        if self._active_job is not None:
            self._cancel_job()
        if self.backend is not None:
            self.backend.close()
        # Temporary files staged for drag-and-drop belong to this window.
        self.drag_out.cleanup()
        if not self.is_maximized():
            width, height = self.get_default_size()
            self.settings["window_width"] = width
            self.settings["window_height"] = height
        self.settings["window_maximized"] = self.is_maximized()
        return False


# -- helpers -------------------------------------------------------------


def _describe(nodes: list[Node]) -> str:
    if len(nodes) == 1:
        return f"“{nodes[0].name}”"
    return format_count(len(nodes), "item")


def _open_folder(path: str) -> None:
    launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(path))
    launcher.launch(None, None, None)


def _archive_filters() -> Gio.ListStore:
    store = Gio.ListStore(item_type=Gtk.FileFilter)

    everything = Gtk.FileFilter(name="All archives")
    for fmt in detect.FORMATS.values():
        for ext in fmt.extensions:
            everything.add_pattern(f"*{ext}")
            everything.add_pattern(f"*{ext.upper()}")
    store.append(everything)

    for key in ("zip", "7z", "rar", "tar.gz", "iso"):
        fmt = detect.FORMATS[key]
        single = Gtk.FileFilter(name=fmt.label)
        for ext in fmt.extensions:
            single.add_pattern(f"*{ext}")
        store.append(single)

    any_file = Gtk.FileFilter(name="All files")
    any_file.add_pattern("*")
    store.append(any_file)
    return store
