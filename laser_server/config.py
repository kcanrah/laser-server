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
    jobs_dir: Path = Path(_env("LASER_JOBS_DIR", str(Path.home() / "laser-jobs"))).expanduser()
    # If set, the UI and API require HTTP basic auth with this password (any username).
    password: str = _env("LASER_PASSWORD", "")
    max_upload_mb: int = int(_env("LASER_MAX_UPLOAD_MB", "200"))
