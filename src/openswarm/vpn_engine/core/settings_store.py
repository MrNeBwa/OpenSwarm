"""Persistent, validated settings owned by the core process."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..schema import (
    Settings,
    SettingsError,
    default_settings_path,
    load_settings,
    save_settings,
)


class SettingsStore:
    """Load/save ``settings.json`` and keep the current object in memory.

    The public schema module owns validation and atomic writes; this class
    adds the core policy: create a private default file on first use and never
    overwrite a file written by a newer schema version.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_settings_path()
        self._lock = threading.RLock()
        self._settings: Settings | None = None

    def load(self) -> Settings:
        with self._lock:
            try:
                settings = load_settings(self.path)
            except SettingsError as exc:
                if not self.path.exists() and "not found" in str(exc):
                    settings = Settings()
                    save_settings(settings, self.path)
                else:
                    raise
            self._settings = settings
            return settings

    def get(self) -> Settings:
        with self._lock:
            if self._settings is None:
                return self.load()
            return self._settings

    def save(self, settings: Settings) -> Settings:
        if not isinstance(settings, Settings):
            raise TypeError("settings must be a Settings instance")
        with self._lock:
            settings.validate()
            save_settings(settings, self.path)
            self._settings = settings
            return settings

    def update(self, patch: Mapping[str, Any], *, merge: bool = True) -> Settings:
        """Apply a validated patch and persist it.

        ``merge=False`` treats the patch as a complete settings object.
        Validation happens before the file is touched, so an invalid patch
        never changes the on-disk state.
        """

        if not isinstance(patch, Mapping):
            raise TypeError("patch must be a mapping")
        with self._lock:
            current = self.get()
            if merge:
                merged = current.to_dict()
                merged.update(dict(patch))
            else:
                merged = dict(patch)
            settings = Settings.from_dict(merged, strict=True)
            return self.save(settings)

    def reload(self) -> Settings:
        with self._lock:
            self._settings = None
            return self.load()

    def reset(self) -> Settings:
        return self.save(Settings())


__all__ = ["SettingsStore"]
