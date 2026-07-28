"""Password prompts, collision prompts and error reporting.

Every user-facing failure funnels through :func:`present_error`, which knows how
to turn an :class:`ArchiveError` into a message, a hint and a collapsed
"Technical details" section — so the person sees a sentence, not a traceback.
"""

from __future__ import annotations

import os

from gi.repository import Adw, GLib, Gtk

from ..core.conflict import BlockingResolver, Resolution
from ..core.errors import ArchiveError, MissingTool
from .utils import format_date_long, format_size


def present_error(parent: Gtk.Widget, error: BaseException,
                  title: str = "Something went wrong") -> None:
    """Show an error the user can actually understand and act on."""
    if isinstance(error, ArchiveError):
        message = error.message
        hint = error.hint
        detail = error.detail
    else:
        message = "ArchiveFree ran into an unexpected problem."
        hint = "If this keeps happening, please report it as a bug."
        detail = f"{type(error).__name__}: {error}"

    dialog = Adw.AlertDialog(heading=title, body=message)
    dialog.add_response("close", "Close")
    dialog.set_default_response("close")
    dialog.set_close_response("close")

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

    if hint:
        hint_label = Gtk.Label(label=hint, wrap=True, xalign=0.0,
                               justify=Gtk.Justification.LEFT)
        hint_label.add_css_class("dim-label")
        box.append(hint_label)

    if isinstance(error, MissingTool) and error.hint:
        # The install command is the whole point of this dialog: make it copyable.
        command = error.hint.split("Install it with:", 1)[-1].strip()
        box.append(_copyable_command(command))
        dialog.add_response("copy", "Copy Command")
        dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", _on_copy_response, command)

    if detail:
        expander = Gtk.Expander(label="Technical details")
        detail_view = Gtk.TextView(
            editable=False, cursor_visible=False, monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8,
            left_margin=8, right_margin=8,
        )
        detail_view.get_buffer().set_text(detail.strip())
        scroller = Gtk.ScrolledWindow(
            min_content_height=90, max_content_height=200, propagate_natural_height=True,
        )
        scroller.set_child(detail_view)
        scroller.add_css_class("card")
        expander.set_child(scroller)
        box.append(expander)

    if box.get_first_child():
        dialog.set_extra_child(box)

    dialog.present(parent)


def _copyable_command(command: str) -> Gtk.Widget:
    label = Gtk.Label(label=command, selectable=True, xalign=0.0, wrap=True,
                      wrap_mode=2)
    label.add_css_class("monospace")
    frame = Gtk.Frame()
    frame.add_css_class("af-command")
    frame.set_child(label)
    return frame


def _on_copy_response(dialog: Adw.AlertDialog, response: str, command: str) -> None:
    if response == "copy":
        display = dialog.get_display() if hasattr(dialog, "get_display") else None
        clipboard = display.get_clipboard() if display else None
        if clipboard:
            clipboard.set(command)


# -- passwords -----------------------------------------------------------


def ask_password(parent: Gtk.Widget, archive_name: str, callback,
                 retry: bool = False) -> None:
    """Prompt for a password. ``callback(password_or_None)`` on the main loop."""
    dialog = Adw.AlertDialog(
        heading="Password Required" if not retry else "Incorrect Password",
        body=(
            f"“{archive_name}” is protected. Enter its password to see what’s inside."
            if not retry else
            f"That password didn’t unlock “{archive_name}”. Passwords are case-sensitive."
        ),
    )
    entry = Adw.PasswordEntryRow(title="Password")
    entry.set_show_apply_button(False)
    group = Adw.PreferencesGroup()
    group.add(entry)
    dialog.set_extra_child(group)

    dialog.add_response("cancel", "Cancel")
    dialog.add_response("unlock", "Unlock")
    dialog.set_response_appearance("unlock", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("unlock")
    dialog.set_close_response("cancel")

    def respond(_dialog, response: str) -> None:
        callback(entry.get_text() if response == "unlock" else None)

    dialog.connect("response", respond)
    # Enter in the entry should submit rather than doing nothing.
    entry.connect("entry-activated", lambda *_: dialog.close_and_respond("unlock"))
    dialog.present(parent)
    GLib.idle_add(entry.grab_focus)


# -- collisions ----------------------------------------------------------


class DialogConflictResolver(BlockingResolver):
    """Asks the user, on the main loop, what to do about an existing file.

    The worker thread blocks inside
    :meth:`~archivefree.core.conflict.BlockingResolver.ask` while this runs, so
    the window stays responsive and the operation stays cancellable.
    """

    def __init__(self, parent: Gtk.Widget):
        super().__init__(presenter=self._present)
        self._parent = parent

    def _present(self, target: str, entry, resolver) -> None:
        from ..core.jobs import _to_main_thread

        _to_main_thread(self._show, target, entry, resolver)

    def _show(self, target: str, entry, resolver) -> None:
        name = os.path.basename(target)
        dialog = Adw.AlertDialog(
            heading="A file with this name already exists",
            body=f"“{name}” is already in that folder. What would you like to do?",
        )
        dialog.set_extra_child(_comparison(target, entry))

        dialog.add_response("cancel", "Cancel")
        dialog.add_response("skip", "Skip")
        dialog.add_response("rename", "Keep Both")
        dialog.add_response("replace", "Replace")
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        check = Gtk.CheckButton(label="Apply this to everything else")
        check.set_margin_top(6)
        extra = dialog.get_extra_child()
        if isinstance(extra, Gtk.Box):
            extra.append(check)

        mapping = {
            "replace": Resolution.OVERWRITE,
            "skip": Resolution.SKIP,
            "rename": Resolution.RENAME,
            "cancel": Resolution.CANCEL,
        }

        def respond(_dialog, response: str) -> None:
            resolution = mapping.get(response, Resolution.CANCEL)
            resolver.answer(resolution,
                            apply_to_all=check.get_active() and resolution
                            is not Resolution.CANCEL)

        dialog.connect("response", respond)
        dialog.present(self._parent)


def _comparison(target: str, entry) -> Gtk.Widget:
    """Show both versions side by side so the choice is an informed one."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    group = Adw.PreferencesGroup()

    try:
        stat = os.stat(target)
        import datetime

        existing_desc = (
            f"{format_size(stat.st_size)} · "
            f"{format_date_long(datetime.datetime.fromtimestamp(stat.st_mtime))}"
        )
    except OSError:
        existing_desc = "Already on disk"

    incoming_desc = f"{format_size(entry.size)} · {format_date_long(entry.modified)}"

    existing_row = Adw.ActionRow(title="Already in that folder", subtitle=existing_desc)
    existing_row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic"))
    incoming_row = Adw.ActionRow(title="From this archive", subtitle=incoming_desc)
    incoming_row.add_prefix(Gtk.Image.new_from_icon_name("package-x-generic-symbolic"))

    group.add(existing_row)
    group.add(incoming_row)
    box.append(group)
    return box


# -- confirmations -------------------------------------------------------


def confirm(parent: Gtk.Widget, heading: str, body: str, confirm_label: str,
            callback, destructive: bool = False) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("go", confirm_label)
    dialog.set_response_appearance(
        "go",
        Adw.ResponseAppearance.DESTRUCTIVE if destructive
        else Adw.ResponseAppearance.SUGGESTED,
    )
    dialog.set_default_response("go")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: callback(r == "go"))
    dialog.present(parent)
