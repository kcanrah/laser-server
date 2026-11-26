"""Material presets: named power/speed/etc. settings for a layer, stored on the Pi
in a JSON file so every browser sees the same list."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

SETTING_KEYS = ("mode", "power", "min_power", "speed", "passes", "hatch", "angle",
                "dither", "overscan", "bidirectional")
MODES = ("line", "fill", "fill+line", "image")


class PresetError(ValueError):
    pass


def clean_settings(raw: dict) -> dict:
    """Keep known layer settings and check them (speed is mm/min, like layers)."""
    s = {k: raw[k] for k in SETTING_KEYS if k in raw and raw[k] is not None}
    if s.get("mode") not in MODES:
        raise PresetError("Preset needs a mode: line, fill, fill+line or image")
    try:
        for k in ("power", "min_power"):
            if k in s:
                s[k] = min(100.0, max(0.0, float(s[k])))
        s["speed"] = float(s.get("speed", 0))
        if s["speed"] <= 0:
            raise PresetError("Speed must be > 0")
        s["passes"] = int(s.get("passes", 1))
        if not 1 <= s["passes"] <= 100:
            raise PresetError("Passes must be 1-100")
        for k in ("hatch", "angle", "overscan"):
            if k in s:
                s[k] = float(s[k])
        if "hatch" in s and s["hatch"] <= 0:
            raise PresetError("Line interval must be > 0")
    except (TypeError, ValueError) as e:
        if isinstance(e, PresetError):
            raise
        raise PresetError(f"Bad preset value: {e}") from e
    if "dither" in s and s["dither"] not in ("floyd", "threshold", "grayscale"):
        raise PresetError("Unknown dither mode")
    if "bidirectional" in s:
        s["bidirectional"] = bool(s["bidirectional"])
    return s


class PresetStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
            return data.get("presets", []) if isinstance(data, dict) else []
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise PresetError(f"Presets file is unreadable: {e}") from e

    def _write(self, presets: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "presets": presets}, indent=1))
        tmp.replace(self.path)

    def list(self) -> list[dict]:
        with self._lock:
            presets = self._read()
        return sorted(presets, key=lambda p: (p.get("material", "").lower(), p.get("name", "").lower()))

    def save(self, data: dict, preset_id: Optional[str] = None) -> dict:
        name = str(data.get("name", "")).strip()[:80]
        material = str(data.get("material", "")).strip()[:80]
        if not name:
            raise PresetError("Give the preset a name (e.g. 'Cut' or 'Engrave dark')")
        preset = {
            "id": preset_id or uuid.uuid4().hex[:12],
            "name": name,
            "material": material,
            "notes": str(data.get("notes", "")).strip()[:500],
            "settings": clean_settings(data.get("settings") or {}),
            "updated": time.time(),
        }
        with self._lock:
            presets = self._read()
            if preset_id is not None:
                if not any(p["id"] == preset_id for p in presets):
                    raise PresetError("No such preset")
                presets = [preset if p["id"] == preset_id else p for p in presets]
            else:
                presets.append(preset)
            self._write(presets)
        return preset

    def delete(self, preset_id: str) -> None:
        with self._lock:
            presets = self._read()
            keep = [p for p in presets if p["id"] != preset_id]
            if len(keep) == len(presets):
                raise PresetError("No such preset")
            self._write(keep)

    def import_many(self, items: list, replace: bool = False) -> int:
        """Add presets from an export. Presets with the same material + name are updated."""
        if not isinstance(items, list):
            raise PresetError("Expected a list of presets")
        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            cleaned.append({
                "name": str(it.get("name", "")).strip()[:80],
                "material": str(it.get("material", "")).strip()[:80],
                "notes": str(it.get("notes", "")).strip()[:500],
                "settings": clean_settings(it.get("settings") or {}),
            })
        with self._lock:
            presets = [] if replace else self._read()
            index = {(p["material"].lower(), p["name"].lower()): i for i, p in enumerate(presets)}
            for c in cleaned:
                if not c["name"]:
                    continue
                key = (c["material"].lower(), c["name"].lower())
                if key in index:
                    old = presets[index[key]]
                    presets[index[key]] = {**old, **c, "updated": time.time()}
                else:
                    presets.append({"id": uuid.uuid4().hex[:12], **c, "updated": time.time()})
                    index[key] = len(presets) - 1
            self._write(presets)
        return len(cleaned)
