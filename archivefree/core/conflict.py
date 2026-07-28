"""Name-collision handling.

The app never silently overwrites. When a target exists, the backend calls the
resolver, which (in the GUI) blocks the worker thread while the main loop shows
a dialog. "Apply to all" is remembered here so the user is asked once, not
once per file.
"""

from __future__ import annotations

import enum
import os
import threading


class Resolution(enum.Enum):
    OVERWRITE = "overwrite"
    SKIP = "skip"
    RENAME = "rename"      # write alongside as "name (2).txt"
    CANCEL = "cancel"


class ConflictResolver:
    """Base resolver: remembers an "apply to all" choice.

    The core uses this directly in headless contexts (tests, CLI); the GUI
    subclasses it to actually ask.
    """

    def __init__(self, default: Resolution = Resolution.RENAME):
        self.default = default
        self._remembered: Resolution | None = None

    def resolve(self, target: str, entry) -> Resolution:
        if self._remembered is not None:
            return self._remembered
        return self.ask(target, entry)

    def ask(self, target: str, entry) -> Resolution:
        return self.default

    def remember(self, resolution: Resolution) -> None:
        self._remembered = resolution

    def reset(self) -> None:
        self._remembered = None


class BlockingResolver(ConflictResolver):
    """Bridges a worker thread to a dialog on the main loop.

    :meth:`ask` parks the worker on an Event; the UI calls :meth:`answer` from
    the main loop when the user picks something. Because only the worker blocks,
    the interface stays live and the operation stays cancellable.
    """

    def __init__(self, presenter):
        super().__init__()
        self._presenter = presenter
        self._event = threading.Event()
        self._answer: Resolution = Resolution.SKIP

    def ask(self, target: str, entry) -> Resolution:
        self._event.clear()
        self._presenter(target, entry, self)
        self._event.wait()
        return self._answer

    def answer(self, resolution: Resolution, apply_to_all: bool = False) -> None:
        self._answer = resolution
        if apply_to_all:
            self.remember(resolution)
        self._event.set()

    def abort(self) -> None:
        """Unblock the worker if the window goes away mid-prompt."""
        self._answer = Resolution.CANCEL
        self._event.set()


def unique_path(path: str) -> str:
    """Return ``path`` if free, else "name (2).ext", "name (3).ext"...

    Matches the numbering scheme GNOME Files and Thunar use, so the result
    looks like something the desktop produced rather than a backup file.
    """
    if not os.path.exists(path):
        return path
    directory, name = os.path.split(path)
    stem, dot, ext = name.partition(".")
    # Preserve multi-part extensions like ".tar.gz"
    if dot:
        ext = dot + ext
    counter = 2
    while True:
        candidate = os.path.join(directory, f"{stem} ({counter}){ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1
