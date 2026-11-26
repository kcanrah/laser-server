"""Settings, read from environment variables (see deploy/laser-server.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    host: str = _env("LASER_HOST", "0.0.0.0")
    http_port: int = int(_env("LASER_HTTP_PORT", "8000"))
    serial_port: str = _env("LASER_SERIAL_PORT", "auto")
    baud: int = int(_env("LASER_BAUD", "115200"))
    camera_device: str = _env("LASER_CAMERA", "0")
    camera_width: int = int(_env("LASER_CAMERA_WIDTH", "1280"))
    camera_height: int = int(_env("LASER_CAMERA_HEIGHT", "720"))
    camera_fps: int = int(_env("LASER_CAMERA_FPS", "10"))
    designs_dir: Path = Path(_env("LASER_DESIGNS_DIR", str(Path.home() / "laser-designs"))).expanduser()
    # Bed size in mm, used when GRBL doesn't report $130/$131 max travel.
    bed_width: float = float(_env("LASER_BED_WIDTH", "400"))
    bed_height: float = float(_env("LASER_BED_HEIGHT", "400"))
    # Max power S value when GRBL doesn't report $30.
    s_max: float = float(_env("LASER_S_MAX", "1000"))
    jobs_dir: Path = Path(_env("LASER_JOBS_DIR", str(Path.home() / "laser-jobs"))).expanduser()
    # If set, the UI and API require HTTP basic auth with this password (any username).
    password: str = _env("LASER_PASSWORD", "")
    # Laser power (% of $30) used for "Frame with laser on"; capped at 5%.
    frame_power: float = float(_env("LASER_FRAME_POWER", "1"))
    max_upload_mb: int = int(_env("LASER_MAX_UPLOAD_MB", "200"))
