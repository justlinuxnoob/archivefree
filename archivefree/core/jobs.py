"""Background work with progress and cancellation.

Every archive operation that could take longer than a frame runs through here.
The rule the whole app follows: **backends never touch GTK**. A backend reports
progress by calling ``progress.step()``; the job wrapper marshals that onto the
GTK main loop with ``GLib.idle_add``. That keeps the core importable and
testable with no display attached, which is how the test-suite runs.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import OperationCancelled


class Cancelled(Exception):
    """Raised inside a worker thread when cancellation is requested."""


@dataclass
class Progress:
    """Progress reporter handed to backends.

    Backends call :meth:`step` or :meth:`set_fraction` as often as they like;
    this class rate-limits notifications to ~20/second so that extracting
    50 000 tiny files doesn't spend all its time repainting a progress bar.
    """

    total: int = 0
    current: int = 0
    message: str = ""
    _callback: Callable[[Progress], None] | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)
    _last_emit: float = 0.0
    _min_interval: float = 0.05
    #: Set by the backend so cancellation can kill a subprocess promptly.
    _on_cancel: list[Callable[[], None]] = field(default_factory=list)

    # -- cancellation ----------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()
        for hook in list(self._on_cancel):
            try:
                hook()
            except Exception:
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        """Raise :class:`Cancelled` if the user has asked to stop."""
        if self._cancel.is_set():
            raise Cancelled()

    def on_cancel(self, hook: Callable[[], None]) -> None:
        """Register a hook (e.g. ``proc.terminate``) to run on cancellation."""
        self._on_cancel.append(hook)
        if self._cancel.is_set():
            hook()

    # -- reporting -------------------------------------------------------
    def begin(self, total: int, message: str = "") -> None:
        self.total = total
        self.current = 0
        self.message = message
        self._emit(force=True)

    def step(self, amount: int = 1, message: str | None = None) -> None:
        self.check()
        self.current += amount
        if message is not None:
            self.message = message
        self._emit()

    def set_fraction(self, fraction: float, message: str | None = None) -> None:
        self.check()
        self.total = 1000
        self.current = int(max(0.0, min(1.0, fraction)) * 1000)
        if message is not None:
            self.message = message
        self._emit()

    def set_message(self, message: str) -> None:
        self.check()
        self.message = message
        self._emit()

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.current / self.total)

    def _emit(self, force: bool = False) -> None:
        if self._callback is None:
            return
        now = time.monotonic()
        if force or (now - self._last_emit) >= self._min_interval:
            self._last_emit = now
            self._callback(self)


class Job:
    """A unit of work running on a worker thread.

    Results come back through callbacks that are always invoked on the GTK main
    loop, so handlers can touch widgets directly. When GTK isn't available (the
    test suite), callbacks fire on the worker thread instead.
    """

    def __init__(
        self,
        func: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
        **kwargs: Any,
    ):
        self._func = func
        self._args = args
        self._kwargs = kwargs
        self._on_done = on_done
        self._on_error = on_error
        self._on_cancelled = on_cancelled
        self.progress = Progress()
        if on_progress is not None:
            self.progress._callback = lambda p: _to_main_thread(on_progress, p)
        self._thread: threading.Thread | None = None
        self.finished = threading.Event()

    def start(self) -> Job:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"af-job-{self._func.__name__}")
        self._thread.start()
        return self

    def cancel(self) -> None:
        self.progress.cancel()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            result = self._func(*self._args, progress=self.progress, **self._kwargs)
        except (Cancelled, OperationCancelled):
            if self._on_cancelled:
                _to_main_thread(self._on_cancelled)
        except BaseException as exc:
            if self._on_error:
                _to_main_thread(self._on_error, exc)
        else:
            if self._on_done:
                _to_main_thread(self._on_done, result)
        finally:
            self.finished.set()


#: Set to True by the GTK application at startup. It is *not* inferred from
#: whether GLib imports: PyGObject is importable in the test suite too, and
#: queueing callbacks onto a main loop that will never run would silently
#: swallow every result and error.
_main_loop_active = False


def use_main_loop(active: bool = True) -> None:
    """Route job callbacks through ``GLib.idle_add``. Called by the GTK app."""
    global _main_loop_active
    _main_loop_active = active


def _to_main_thread(func: Callable[..., Any], *args: Any) -> None:
    """Run ``func`` on the GTK main loop if one is running, else run it now."""
    if not _main_loop_active:
        func(*args)
        return

    from gi.repository import GLib

    def invoke(*_: Any) -> bool:
        func(*args)
        return False  # GLib.SOURCE_REMOVE

    GLib.idle_add(invoke, priority=GLib.PRIORITY_DEFAULT_IDLE)
