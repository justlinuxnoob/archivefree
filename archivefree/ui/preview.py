"""Previewing a file without extracting the archive.

Only the first few megabytes are read, and only for kinds we can render
usefully: text, source code and images. Anything else shows its details and an
"Extract This File" button, which is more honest than rendering mojibake.
"""

from __future__ import annotations

from gi.repository import Adw, GdkPixbuf, GLib, Gtk

from ..core.jobs import Progress
from ..core.tree import Node
from .dialogs import present_error
from .utils import describe_type, format_date_long, format_size, is_probably_text

#: Reading more than this into a preview isn't useful and costs real time.
PREVIEW_LIMIT = 4 * 1024 * 1024
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "ico", "tiff", "avif"}


class PreviewDialog(Adw.Dialog):
    __gtype_name__ = "ArchiveFreePreviewDialog"

    def __init__(self, window, node: Node):
        super().__init__(title=node.name, content_width=820, content_height=540)
        self.window = window
        self.node = node

        self.toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(
            title=node.name,
            subtitle=f"{format_size(node.size)} · {describe_type(node.name, False)}",
        ))
        extract_button = Gtk.Button(label="Extract This File")
        extract_button.add_css_class("suggested-action")
        extract_button.connect("clicked", self._on_extract)
        header.pack_end(extract_button)
        self.toolbar.add_top_bar(header)

        self.stack = Gtk.Stack()
        self.stack.add_named(self._spinner(), "loading")
        self.toolbar.set_content(self.stack)
        self.stack.set_visible_child_name("loading")
        self.set_child(self.toolbar)

        self._load()

    def _spinner(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        box.append(Gtk.Spinner(spinning=True, width_request=28, height_request=28))
        box.append(Gtk.Label(label="Reading…"))
        return box

    def _load(self) -> None:
        backend = self.window.backend
        entry = self.node.entry
        if backend is None or entry is None:
            self._show_unsupported("This item can’t be previewed.")
            return

        from ..core.jobs import Job

        def work(progress: Progress) -> bytes:
            return backend.read_member(entry, limit=PREVIEW_LIMIT)

        def done(data: bytes) -> None:
            self._render(data)

        def failed(exc: BaseException) -> None:
            self.close()
            present_error(self.window, exc, title="Can’t Preview This File")

        Job(work, on_done=done, on_error=failed).start()

    # ------------------------------------------------------------------
    def _render(self, data: bytes) -> None:
        extension = self.node.name.rsplit(".", 1)[-1].lower() if "." in self.node.name else ""

        if extension in IMAGE_EXTENSIONS:
            widget = self._try_image(data)
            if widget is not None:
                self.stack.add_named(widget, "image")
                self.stack.set_visible_child_name("image")
                return

        if is_probably_text(data):
            self.stack.add_named(self._text_view(data), "text")
            self.stack.set_visible_child_name("text")
            return

        self._show_unsupported(
            "ArchiveFree can preview text files, code and images. "
            "Extract this file to open it in another application."
        )

    def _try_image(self, data: bytes) -> Gtk.Widget | None:
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
        except GLib.Error:
            return None
        if pixbuf is None:
            return None

        picture = Gtk.Picture.new_for_pixbuf(pixbuf)
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(picture)
        scroller.add_css_class("af-preview-image")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(scroller)
        caption = Gtk.Label(
            label=f"{pixbuf.get_width()} × {pixbuf.get_height()} pixels")
        caption.add_css_class("dim-label")
        caption.add_css_class("caption")
        caption.set_margin_top(6)
        caption.set_margin_bottom(6)
        box.append(caption)
        return box

    def _text_view(self, data: bytes) -> Gtk.Widget:
        truncated = len(data) >= PREVIEW_LIMIT
        text = data.decode("utf-8", "replace")

        view = Gtk.TextView(
            editable=False, cursor_visible=False, monospace=True,
            wrap_mode=Gtk.WrapMode.NONE,
            top_margin=12, bottom_margin=12, left_margin=14, right_margin=14,
        )
        view.get_buffer().set_text(text)
        view.add_css_class("af-preview-text")

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(view)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if truncated:
            banner = Adw.Banner(
                title="Showing the first 4 MB of this file.", revealed=True)
            box.append(banner)
        box.append(scroller)
        return box

    def _show_unsupported(self, message: str) -> None:
        status = Adw.StatusPage(
            icon_name="text-x-generic-symbolic",
            title="No Preview Available",
            description=message,
        )
        group = Adw.PreferencesGroup(width_request=380, halign=Gtk.Align.CENTER)
        group.add(Adw.ActionRow(title="Size", subtitle=format_size(self.node.size)))
        group.add(Adw.ActionRow(title="Type",
                                subtitle=describe_type(self.node.name, False)))
        group.add(Adw.ActionRow(title="Modified",
                                subtitle=format_date_long(self.node.modified)))
        group.add(Adw.ActionRow(title="Path in archive", subtitle=self.node.path))
        status.set_child(group)
        self.stack.add_named(status, "unsupported")
        self.stack.set_visible_child_name("unsupported")

    def _on_extract(self, *_args) -> None:
        entry = self.node.entry
        self.close()
        if entry is not None:
            from .extract import ExtractDialog

            ExtractDialog(self.window, entries=[entry],
                          selection_label=f"“{self.node.name}”").present(self.window)
