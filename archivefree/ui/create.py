"""The create-archive dialog.

Designed so the common case is three clicks: drop files in, press Create. Format
and level have sensible defaults; password and splitting are tucked into an
expander so they don't clutter the path most people take.
"""

from __future__ import annotations

import os

from gi.repository import Adw, Gio, GLib, Gtk

from ..config import config
from ..core import detect, tools
from ..core.create import (
    CREATABLE_HINT,
    LEVELS,
    SPLIT_PRESETS,
    CreateOptions,
    create_archive,
    default_archive_name,
)
from ..core.detect import CREATABLE
from ..core.jobs import Job, Progress
from .dialogs import present_error
from .utils import format_count, format_size, home_relative


class CreateDialog(Adw.Dialog):
    __gtype_name__ = "ArchiveFreeCreateDialog"

    def __init__(self, application, sources: list[str], parent_window=None):
        super().__init__(title="New Archive", content_width=560, content_height=680)
        self.application = application
        self.parent_window = parent_window
        self.settings = config()
        self.sources = [os.path.abspath(s) for s in sources]
        self._destination_dir = (
            os.path.dirname(self.sources[0]) if self.sources
            else (self.settings["last_create_dir"] or GLib.get_home_dir())
        )
        self._job: Job | None = None
        self._rows: list[Gtk.Widget] = []
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False,
                               show_start_title_buttons=False)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._on_cancel)
        header.pack_start(self.cancel_button)

        self.create_button = Gtk.Button(label="Create")
        self.create_button.add_css_class("suggested-action")
        self.create_button.connect("clicked", self._on_create)
        header.pack_end(self.create_button)
        self.toolbar.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_form(), "form")
        self.stack.add_named(self._build_working(), "working")
        self.toolbar.set_content(self.stack)
        self.set_child(self.toolbar)
        self._update_name_extension()
        self._refresh_sources()

    def _build_form(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        # -- what goes in
        self.sources_group = Adw.PreferencesGroup(
            title="What to compress",
            header_suffix=self._add_files_button(),
        )
        page.add(self.sources_group)

        # -- name and place
        output_group = Adw.PreferencesGroup(title="Save as")
        self.name_row = Adw.EntryRow(title="Archive name")
        self.name_row.connect("changed", lambda *_: self._update_summary())
        output_group.add(self.name_row)

        self.location_row = Adw.ActionRow(
            title="Location", subtitle=home_relative(self._destination_dir),
            activatable=True,
        )
        self.location_row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose_location)
        self.location_row.add_suffix(choose)
        self.location_row.connect("activated", self._on_choose_location)
        output_group.add(self.location_row)
        page.add(output_group)

        # -- format and level
        format_group = Adw.PreferencesGroup(title="Format")
        self.format_row = Adw.ComboRow(
            title="Archive format",
            model=Gtk.StringList.new([_format_label(k) for k in CREATABLE]),
        )
        saved_format = self.settings["create_format"]
        if saved_format in CREATABLE:
            self.format_row.set_selected(CREATABLE.index(saved_format))
        self.format_row.connect("notify::selected", self._on_format_changed)
        format_group.add(self.format_row)

        self.format_hint = Gtk.Label(xalign=0.0, wrap=True, wrap_mode=2)
        self.format_hint.add_css_class("dim-label")
        self.format_hint.add_css_class("caption")
        self.format_hint.set_margin_top(4)
        format_group.add(self.format_hint)

        self.level_row = Adw.ComboRow(
            title="Compression",
            model=Gtk.StringList.new([label for _, label, _ in LEVELS]),
        )
        saved_level = self.settings["create_level"]
        keys = [key for key, _, _ in LEVELS]
        if saved_level in keys:
            self.level_row.set_selected(keys.index(saved_level))
        self.level_row.connect("notify::selected", lambda *_: self._update_level_hint())
        format_group.add(self.level_row)
        page.add(format_group)

        # -- advanced, folded away
        advanced = Adw.PreferencesGroup(title="Protection and splitting")
        self.password_row = Adw.PasswordEntryRow(title="Password (optional)")
        self.password_row.connect("changed", lambda *_: self._update_password_state())
        advanced.add(self.password_row)

        self.encrypt_names_row = Adw.SwitchRow(
            title="Hide file names too",
            subtitle="Without the password, even the list of files stays secret",
            active=False, sensitive=False,
        )
        advanced.add(self.encrypt_names_row)

        self.split_row = Adw.ComboRow(
            title="Split into parts",
            subtitle="Useful for email or older USB sticks",
            model=Gtk.StringList.new([label for label, _ in SPLIT_PRESETS]),
        )
        advanced.add(self.split_row)
        page.add(advanced)

        # -- live summary
        summary_group = Adw.PreferencesGroup()
        self.summary_label = Gtk.Label(xalign=0.0, wrap=True, wrap_mode=2)
        self.summary_label.add_css_class("dim-label")
        self.summary_label.add_css_class("caption")
        summary_group.add(self.summary_label)
        page.add(summary_group)

        # Populate the format hint and password state for the initial selection,
        # not just on subsequent changes.
        self._on_format_changed()
        self._update_level_hint()
        return page

    def _add_files_button(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=6)
        add_files = Gtk.Button(icon_name="document-open-symbolic",
                               tooltip_text="Add files")
        add_files.add_css_class("flat")
        add_files.connect("clicked", lambda *_: self._pick(folder=False))
        add_folder = Gtk.Button(icon_name="folder-new-symbolic",
                                tooltip_text="Add a folder")
        add_folder.add_css_class("flat")
        add_folder.connect("clicked", lambda *_: self._pick(folder=True))
        box.append(add_files)
        box.append(add_folder)
        return box

    def _build_working(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                      margin_start=36, margin_end=36)
        self.working_title = Gtk.Label(label="Creating archive…")
        self.working_title.add_css_class("title-2")
        self.working_bar = Gtk.ProgressBar(hexpand=True, width_request=340)
        self.working_detail = Gtk.Label(label="Starting…")
        self.working_detail.add_css_class("dim-label")
        box.append(self.working_title)
        box.append(self.working_bar)
        box.append(self.working_detail)
        return box

    # ------------------------------------------------------------------
    # Source list
    # ------------------------------------------------------------------
    def _refresh_sources(self) -> None:
        for row in self._rows:
            self.sources_group.remove(row)
        self._rows = []

        if not self.sources:
            row = Adw.ActionRow(
                title="Nothing chosen yet",
                subtitle="Add files or a folder to compress",
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
            self.sources_group.add(row)
            self._rows.append(row)
        else:
            for source in self.sources:
                is_dir = os.path.isdir(source)
                row = Adw.ActionRow(
                    title=os.path.basename(source.rstrip("/")) or source,
                    subtitle=home_relative(os.path.dirname(source)),
                )
                row.add_prefix(Gtk.Image.new_from_icon_name(
                    "folder-symbolic" if is_dir else "text-x-generic-symbolic"))
                remove = Gtk.Button(icon_name="edit-delete-symbolic",
                                    valign=Gtk.Align.CENTER,
                                    tooltip_text="Remove from this archive")
                remove.add_css_class("flat")
                remove.connect("clicked", self._on_remove_source, source)
                row.add_suffix(remove)
                self.sources_group.add(row)
                self._rows.append(row)

        self._update_name_extension()
        self._update_summary()

    def _on_remove_source(self, _button, source: str) -> None:
        self.sources = [s for s in self.sources if s != source]
        self._refresh_sources()

    def _pick(self, folder: bool) -> None:
        dialog = Gtk.FileDialog(title="Add a folder" if folder else "Add files")
        if os.path.isdir(self._destination_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(self._destination_dir))

        def done(source, result) -> None:
            try:
                if folder:
                    chosen = [source.select_folder_finish(result)]
                else:
                    files = source.open_multiple_finish(result)
                    chosen = [files.get_item(i) for i in range(files.get_n_items())]
            except GLib.Error:
                return
            for file in chosen:
                if file and file.get_path() and file.get_path() not in self.sources:
                    self.sources.append(file.get_path())
            if self.sources and not self._destination_dir:
                self._destination_dir = os.path.dirname(self.sources[0])
            self._refresh_sources()

        if folder:
            dialog.select_folder(self.parent_window or self, None, done)
        else:
            dialog.open_multiple(self.parent_window or self, None, done)

    def _on_choose_location(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Where to save the archive",
                                accept_label="Save Here")
        if os.path.isdir(self._destination_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(self._destination_dir))

        def done(source, result) -> None:
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self._destination_dir = folder.get_path()
                self.location_row.set_subtitle(home_relative(self._destination_dir))
                self._update_summary()

        dialog.select_folder(self.parent_window or self, None, done)

    # ------------------------------------------------------------------
    # Form state
    # ------------------------------------------------------------------
    @property
    def _format_key(self) -> str:
        return CREATABLE[self.format_row.get_selected()]

    @property
    def _level_key(self) -> str:
        return LEVELS[self.level_row.get_selected()][0]

    def _on_format_changed(self, *_args) -> None:
        self._update_name_extension()
        self._update_password_state()
        self._update_summary()

        fmt = self._format_key
        self.format_hint.set_text(CREATABLE_HINT.get(fmt, ""))

        # TAR can't carry a password; say so instead of failing later.
        supports_password = fmt in ("zip", "7z")
        self.password_row.set_sensitive(supports_password)
        if not supports_password:
            self.password_row.set_text("")
            self.password_row.set_title("Password (not supported by this format)")
        else:
            self.password_row.set_title("Password (optional)")

    def _update_level_hint(self) -> None:
        self.level_row.set_subtitle(LEVELS[self.level_row.get_selected()][2])
        self._update_summary()

    def _update_password_state(self) -> None:
        has_password = bool(self.password_row.get_text())
        self.encrypt_names_row.set_sensitive(has_password and self._format_key == "7z")
        if not has_password:
            self.encrypt_names_row.set_active(False)
        self._update_summary()

    def _update_name_extension(self) -> None:
        """Keep the filename's extension in step with the chosen format."""
        fmt = self._format_key
        extension = detect.FORMATS[fmt].extensions[0]
        current = self.name_row.get_text().strip()

        if not current:
            if self.sources:
                self.name_row.set_text(default_archive_name(self.sources, fmt))
            return

        for other in CREATABLE:
            for ext in detect.FORMATS[other].extensions:
                if current.lower().endswith(ext):
                    self.name_row.set_text(current[: -len(ext)] + extension)
                    return
        self.name_row.set_text(current + extension)

    def _update_summary(self) -> None:
        if not self.sources:
            self.summary_label.set_text("Choose at least one file or folder to compress.")
            self.create_button.set_sensitive(False)
            return
        self.create_button.set_sensitive(bool(self.name_row.get_text().strip()))

        count, total = _measure(self.sources)
        target = os.path.join(self._destination_dir, self.name_row.get_text().strip())
        note = f"{format_count(count, 'file')} · {format_size(total)} → {target}"
        if self.password_row.get_text():
            note += "\nProtected with a password. There is no way to recover it if lost."
        self.summary_label.set_text(note)

    # ------------------------------------------------------------------
    # Creating
    # ------------------------------------------------------------------
    def _on_create(self, *_args) -> None:
        name = self.name_row.get_text().strip()
        if not name or not self.sources:
            return
        destination = os.path.join(self._destination_dir, name)

        if os.path.exists(destination):
            from .dialogs import confirm

            confirm(
                self,
                heading="Replace existing archive?",
                body=f"“{name}” already exists in that folder. "
                     "Creating this archive will overwrite it.",
                confirm_label="Replace",
                destructive=True,
                callback=lambda ok: self._start(destination) if ok else None,
            )
            return
        self._start(destination)

    def _start(self, destination: str) -> None:
        fmt = self._format_key
        level = self._level_key
        password = self.password_row.get_text() or None
        split_bytes = SPLIT_PRESETS[self.split_row.get_selected()][1]

        if (password or split_bytes) and not tools.sevenzip():
            from ..core.errors import MissingTool

            present_error(
                self.parent_window or self,
                MissingTool("7z", "make password-protected or split archives", "7zip"),
                title="A Helper Program Is Needed",
            )
            return

        # Store paths relative to the shared parent so the archive doesn't
        # contain "home/you/Documents/..." for everyone who opens it.
        base_dir = os.path.dirname(self.sources[0]) if len(self.sources) == 1 \
            else os.path.commonpath([os.path.dirname(s) for s in self.sources])

        options = CreateOptions(
            destination=destination, format=fmt, level=level, password=password,
            encrypt_names=self.encrypt_names_row.get_active(),
            split_bytes=split_bytes, base_dir=base_dir,
        )

        self.settings["create_format"] = fmt
        self.settings["create_level"] = level
        self.settings["last_create_dir"] = self._destination_dir

        self.stack.set_visible_child_name("working")
        self.create_button.set_visible(False)
        self.cancel_button.set_label("Cancel")
        self.set_can_close(False)
        self.working_title.set_text(f"Creating {os.path.basename(destination)}…")

        sources = list(self.sources)

        def work(progress: Progress) -> str:
            return create_archive(sources, options, progress=progress)

        def done(result: str) -> None:
            self.set_can_close(True)
            self._job = None
            self.close()
            window = self.parent_window
            if window is not None:
                size = os.path.getsize(result) if os.path.exists(result) else 0
                toast = Adw.Toast(
                    title=f"Created {os.path.basename(result)} ({format_size(size)})",
                    timeout=6,
                )
                toast.set_button_label("Open")
                toast.connect("button-clicked",
                              lambda *_: self.application.open_paths([result]))
                window.toast_overlay.add_toast(toast)
            else:
                self.application.open_paths([result])

        def failed(exc: BaseException) -> None:
            self.set_can_close(True)
            self._job = None
            self.stack.set_visible_child_name("form")
            self.create_button.set_visible(True)
            present_error(self.parent_window or self, exc,
                          title="Couldn’t Create the Archive")

        def cancelled() -> None:
            self.set_can_close(True)
            self._job = None
            self.close()

        def on_progress(progress: Progress) -> None:
            if progress.total > 0:
                self.working_bar.set_fraction(progress.fraction)
                self.working_detail.set_text(
                    f"{progress.message}  ·  {progress.fraction:.0%}"
                    if progress.message else f"{progress.fraction:.0%}"
                )
            else:
                self.working_bar.pulse()
                self.working_detail.set_text(progress.message or "Working…")

        self._job = Job(work, on_done=done, on_error=failed, on_cancelled=cancelled,
                        on_progress=on_progress).start()

    def _on_cancel(self, *_args) -> None:
        if self._job is not None:
            self.cancel_button.set_sensitive(False)
            self.cancel_button.set_label("Cancelling…")
            self._job.cancel()
        else:
            self.close()


def _measure(sources: list[str]) -> tuple[int, int]:
    """Count files and total bytes, capped so a huge tree doesn't stall the UI."""
    count = 0
    total = 0
    for source in sources:
        if os.path.isfile(source):
            count += 1
            try:
                total += os.path.getsize(source)
            except OSError:
                pass
            continue
        for dirpath, _, filenames in os.walk(source):
            for name in filenames:
                count += 1
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
            if count > 200_000:
                return count, total
    return count, total


def _format_label(key: str) -> str:
    labels = {
        "zip": "ZIP — opens everywhere",
        "7z": "7z — smallest files",
        "tar.gz": "TAR.GZ — the Linux standard",
        "tar.xz": "TAR.XZ — smaller, slower",
        "tar.zst": "TAR.ZST — small and fast",
        "tar.bz2": "TAR.BZ2 — older, widely supported",
        "tar": "TAR — no compression",
    }
    return labels.get(key, key.upper())
