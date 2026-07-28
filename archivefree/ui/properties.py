"""Archive properties: format, sizes, compression ratio, volumes, backend."""

from __future__ import annotations

import os

from gi.repository import Adw, Gtk

from ..core import tools
from ..core.entry import ArchiveInfo
from .utils import format_count, format_size, home_relative


class PropertiesDialog(Adw.Dialog):
    __gtype_name__ = "ArchiveFreePropertiesDialog"

    def __init__(self, info: ArchiveInfo, backend=None):
        super().__init__(title="Archive Properties", content_width=480)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        page = Adw.PreferencesPage()

        general = Adw.PreferencesGroup(title="General")
        general.add(_row("Name", os.path.basename(info.path)))
        general.add(_row("Location", home_relative(os.path.dirname(info.path))))
        general.add(_row("Format", info.format_label))
        page.add(general)

        contents = Adw.PreferencesGroup(title="Contents")
        contents.add(_row("Files", format_count(info.entry_count, "file")))
        contents.add(_row("Size on disk", format_size(info.archive_size)))
        contents.add(_row("Size once extracted", format_size(info.total_size)))
        if info.total_size and info.archive_size:
            ratio = info.ratio
            contents.add(_row(
                "Compression",
                f"{ratio:.1%} smaller" if ratio > 0
                else "Stored without compression",
            ))
        page.add(contents)

        if info.volumes:
            volumes = Adw.PreferencesGroup(
                title="Split Archive",
                description="All parts must stay in the same folder.",
            )
            for volume in info.volumes:
                volumes.add(_row(os.path.basename(volume),
                                 format_size(os.path.getsize(volume))
                                 if os.path.exists(volume) else "Missing"))
            page.add(volumes)

        security = Adw.PreferencesGroup(title="Security")
        security.add(_row("Password protected", "Yes" if info.encrypted else "No"))
        page.add(security)

        if info.comment:
            comment_group = Adw.PreferencesGroup(title="Comment")
            label = Gtk.Label(label=info.comment, wrap=True, xalign=0.0, selectable=True)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            comment_group.add(label)
            page.add(comment_group)

        backend_group = Adw.PreferencesGroup(
            title="Handled By",
            description="Which part of ArchiveFree reads this format.",
        )
        backend_group.add(_row("Backend", type(backend).__name__.replace("Backend", "")
                               if backend else "Unknown"))
        if backend is not None and type(backend).__name__ == "SevenZipBackend":
            backend_group.add(_row("Tool", tools.sevenzip_version() or "7-Zip"))
        page.add(backend_group)

        toolbar.set_content(page)
        self.set_child(toolbar)


def _row(title: str, subtitle: str) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    row.set_subtitle_selectable(True)
    return row
