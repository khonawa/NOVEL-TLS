from __future__ import annotations

import json

from .constants import CHUNK_PRESETS, SETTINGS_PATH


def load_settings() -> dict[str, object]:
    defaults: dict[str, object] = {
        "scroll_interval": 0.2,
        "required_sweeps": 2,
        "chunk_preset": "Balanced (275k)",
        "input_dir": "",
        "output_dir": "",
        "geometry": "820x680",
    }
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            defaults.update(saved)
    except (OSError, json.JSONDecodeError):
        pass
    try:
        defaults["scroll_interval"] = max(0.02, min(2.0, float(defaults["scroll_interval"])))
        defaults["required_sweeps"] = max(1, min(10, int(defaults["required_sweeps"])))
    except (TypeError, ValueError):
        defaults["scroll_interval"] = 0.2
        defaults["required_sweeps"] = 2
    if defaults["chunk_preset"] not in CHUNK_PRESETS:
        defaults["chunk_preset"] = "Balanced (275k)"
    for key in ("input_dir", "output_dir", "geometry"):
        if not isinstance(defaults[key], str):
            defaults[key] = "" if key != "geometry" else "980x760"
    return defaults

def save_settings(settings: dict[str, object]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_PATH)

def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"
