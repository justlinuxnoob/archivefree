"""Persistent settings, stored as JSON under the XDG config directory.

Deliberately not GSettings: a schema would have to be compiled at install time
in both the .deb and the Flatpak, which is a real source of "installs fine, then
crashes on launch" bugs. There are barely a dozen settings here, so a JSON file
that can never fail to load is the better trade.
"""

from __future__ import annotations

import json
import os
import threading

from gi.repository import GLib

_DEFAULTS: dict[str, object] = {
    "first_run_completed": False,
    "default_handler_offered": False,
    "extract_here_by_default": False,
    "open_folder_after_extract": True,
    "create_subfolder": True,
    "last_extract_dir": "",
    "last_create_dir": "",
    "create_format": "zip",
    "create_level": "normal",
    "window_width": 980,
    "window_height": 640,
    "window_maximized": False,
    "show_compressed_column": True,
    "show_type_column": True,
    "show_modified_column": True,
    "confirm_before_extracting_many": True,
}


class Config:
    """Thread-safe key/value store with atomic writes."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(
            GLib.get_user_config_dir(), "archivefree", "settings.json"
        )
        self._lock = threading.Lock()
        self._data: dict[str, object] = dict(_DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                # Unknown keys are ignored so a downgrade can't break startup.
                for key in _DEFAULTS:
                    if key in stored:
                        self._data[key] = stored[key]
        except (OSError, ValueError):
            pass  # missing or corrupt: defaults are always usable

    def save(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                temp = self.path + ".tmp"
                with open(temp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2, sort_keys=True)
                os.replace(temp, self.path)
            except OSError:
                pass  # settings are a convenience, never a reason to fail

    def get(self, key: str, fallback=None):
        return self._data.get(key, _DEFAULTS.get(key, fallback))

    def set(self, key: str, value) -> None:
        if self._data.get(key) != value:
            self._data[key] = value
            self.save()

    def __getitem__(self, key: str):
        return self.get(key)

    def __setitem__(self, key: str, value) -> None:
        self.set(key, value)


_instance: Config | None = None


def config() -> Config:
    global _instance
    if _instance is None:
        _instance = Config()
    return _instance
