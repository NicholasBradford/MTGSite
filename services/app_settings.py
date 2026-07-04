import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from dotenv import load_dotenv


DEFAULT_SETTINGS = {
    "schema_version": 1,
    "features": {
        "planeswalker_collection": True,
        "trade_screen": True,
        "edh_decks": True,
    },
    "homepage": {
        "color_scheme": "dark_green",
        "default_view_mode": "grid",
    },
    "autosorter": {
        "enabled": True,
        "strategy": "default",
    },
}


def deep_merge(defaults, user_values):
    merged = deepcopy(defaults)

    for key, value in user_values.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value

    return merged


def get_settings_path() -> Path:
    load_dotenv()

    env_path = os.environ.get("MTGSITE_SETTINGS_PATH")
    if env_path:
        return Path(env_path)

    return Path("data/app_settings.json")


class SettingsManager:
    def __init__(self):
        self.path = get_settings_path()
        self._settings = deepcopy(DEFAULT_SETTINGS)
        self._mtime_ns = None
        self._version = 0
        self._lock = threading.Lock()

    def load_initial(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if not self.path.exists():
            self._write_settings_file(deepcopy(DEFAULT_SETTINGS))

        self.reload_if_changed(force=True)

    def reload_if_changed(self, force=False) -> bool:
        """
        Reloads settings only when app_settings.json changed.

        Returns True if settings were reloaded.
        Returns False if nothing changed or reload failed.
        """
        with self._lock:
            try:
                current_mtime_ns = self.path.stat().st_mtime_ns
            except FileNotFoundError:
                self._write_settings_file(deepcopy(DEFAULT_SETTINGS))
                current_mtime_ns = self.path.stat().st_mtime_ns

            if not force and self._mtime_ns == current_mtime_ns:
                return False

            try:
                with self.path.open("r", encoding="utf-8") as f:
                    user_settings = json.load(f)

                merged = deep_merge(DEFAULT_SETTINGS, user_settings)

            except json.JSONDecodeError as exc:
                print(f"[settings] Invalid JSON in {self.path}: {exc}")
                print("[settings] Keeping previous valid settings.")
                return False

            self._settings = merged
            self._mtime_ns = current_mtime_ns
            self._version += 1

            return True

    def _write_settings_file(self, settings: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        temp_path = self.path.with_suffix(".json.tmp")

        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)

        temp_path.replace(self.path)

    def all(self) -> dict:
        return deepcopy(self._settings)

    def version(self) -> int:
        return self._version

    def get(self, section: str, key: str, default=None):
        return self._settings.get(section, {}).get(key, default)

    def feature_enabled(self, feature_name: str) -> bool:
        return bool(
            self._settings
            .get("features", {})
            .get(feature_name, False)
        )


settings_manager = SettingsManager()