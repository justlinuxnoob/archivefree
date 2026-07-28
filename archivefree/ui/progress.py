"""The non-modal progress bar that slides in along the bottom of the window.

Non-modal on purpose: while a 4 GB archive extracts you can still browse the
listing, search it, and read file names. Only the operation itself is busy.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from gi.repository import Gtk

from ..core.jobs import Progress
from .utils import format_size


class ProgressBar(Gtk.Revealer):
    __gtype_name__ = "ArchiveFreeProgressBar"

    def __init__(self, on_cancel: Callable[[], None]):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                         transition_duration=200, reveal_child=False)
        self._started = 0.0
        self._last_bytes = 0
        self._last_rate_update = 0.0
        self._rate = 0.0

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class("af-progress")

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        self.title = Gtk.Label(xalign=0.0, ellipsize=3)
        self.title.add_css_class("heading")
        self.bar = Gtk.ProgressBar(hexpand=True)
        self.bar.add_css_class("af-progress-bar")
        self.detail = Gtk.Label(xalign=0.0, ellipsize=3)
        self.detail.add_css_class("dim-label")
        self.detail.add_css_class("caption")

        text_box.append(self.title)
        text_box.append(self.bar)
        text_box.append(self.detail)

        self.cancel_button = Gtk.Button(label="Cancel", valign=Gtk.Align.CENTER)
        self.cancel_button.add_css_class("destructive-action")
        self.cancel_button.connect("clicked", lambda *_: on_cancel())

        box.append(text_box)
        box.append(self.cancel_button)
        self.set_child(box)

    def begin(self, title: str) -> None:
        self._started = time.monotonic()
        self._last_bytes = 0
        self._rate = 0.0
        self.title.set_text(title)
        self.detail.set_text("Starting…")
        self.bar.set_fraction(0.0)
        self.cancel_button.set_sensitive(True)
        self.cancel_button.set_label("Cancel")
        self.set_reveal_child(True)

    def update(self, progress: Progress) -> None:
        fraction = progress.fraction
        if progress.total <= 0:
            self.bar.pulse()
        else:
            self.bar.set_fraction(fraction)

        parts: list[str] = []
        if progress.message:
            parts.append(progress.message)

        now = time.monotonic()
        elapsed = now - self._started
        # Throughput is only meaningful once there's a second of data behind it.
        if progress.total > 1000 and elapsed > 1.0:
            if now - self._last_rate_update > 0.5:
                delta = progress.current - self._last_bytes
                interval = now - (self._last_rate_update or self._started)
                if interval > 0:
                    self._rate = delta / interval
                self._last_bytes = progress.current
                self._last_rate_update = now
            if self._rate > 0:
                parts.append(f"{format_size(int(self._rate))}/s")
                remaining = progress.total - progress.current
                if remaining > 0:
                    seconds = remaining / self._rate
                    if seconds < 3600:
                        parts.append(f"{_duration(seconds)} left")
        if progress.total > 0:
            parts.append(f"{fraction:.0%}")

        self.detail.set_text(" · ".join(parts) if parts else "Working…")

    def set_cancelling(self) -> None:
        self.cancel_button.set_sensitive(False)
        self.cancel_button.set_label("Cancelling…")
        self.detail.set_text("Stopping…")

    def end(self) -> None:
        self.set_reveal_child(False)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{max(seconds, 1)}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"
