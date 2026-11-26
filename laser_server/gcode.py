"""Small G-code helpers: bounding box of the burn area for framing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from array import array
from typing import Iterator

from .grbl import clean_gcode_line

_WORD_RE = re.compile(r"([A-Z])([-+]?\d*\.?\d+)")


def job_bounds(path: Path) -> Optional[tuple[float, float, float, float]]:
    """Return (xmin, ymin, xmax, ymax) in mm, work coordinates, of the moves
    that burn (G1/G2/G3 with S > 0). Falls back to all G1-G3 moves if the file
    never sets S, then to all moves. Arc bulges beyond endpoints are ignored."""
    absolute = True
    scale = 1.0
    motion = 0
    x = y = 0.0
    cut: list[tuple[float, float]] = []
    burn: list[tuple[float, float]] = []
    power = 0.0
    any_moves: list[tuple[float, float]] = []

    for raw in path.read_text(errors="replace").splitlines():
        line = clean_gcode_line(raw)
        if not line or line.startswith("$"):
            continue
        words = _WORD_RE.findall(line)
        has_xy = False
        nx, ny = x, y
        for letter, val in words:
            v = float(val)
            if letter == "G":
                g = int(v) if v.is_integer() else v
                if g in (0, 1, 2, 3):
                    motion = g
                elif g == 90:
                    absolute = True
                elif g == 91:
                    absolute = False
                elif g == 20:
                    scale = 25.4
                elif g == 21:
                    scale = 1.0
            elif letter == "X":
                nx = v * scale if absolute else x + v * scale
                has_xy = True
            elif letter == "Y":
                ny = v * scale if absolute else y + v * scale
                has_xy = True
            elif letter == "S":
                power = v
        if not has_xy:
            continue
        if motion in (1, 2, 3):
            cut += [(x, y), (nx, ny)]
            if power > 0:
                burn += [(x, y), (nx, ny)]
        any_moves.append((nx, ny))
        x, y = nx, ny

    pts = burn or cut or any_moves
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def iter_job_lines(path: Path) -> Iterator[str]:
    """Cleaned, non-empty G-code lines of a file, read lazily."""
    with path.open(errors="replace") as f:
        for raw in f:
            line = clean_gcode_line(raw)
            if line:
                yield line


def estimate_job(path: Path, rapid_mm_min: float = 6000.0) -> tuple[int, array]:
    """Count the lines GRBL will receive and estimate, per line, the cumulative
    seconds until it finishes (distance / feed; ignores acceleration, so it
    runs a little optimistic and the UI corrects it with real progress)."""
    cum = array("d")
    t = 0.0
    x = y = z = 0.0
    absolute = True
    scale = 1.0
    motion = 0
    feed = 1000.0
    rapid = max(rapid_mm_min, 1.0)
    for line in iter_job_lines(path):
        if line[0] != "$":
            words = _WORD_RE.findall(line)
            nx, ny, nz = x, y, z
            moved = False
            dwell = 0.0
            g4 = False
            for letter, val in words:
                v = float(val)
                if letter == "G":
                    if v in (0, 1, 2, 3):
                        motion = int(v)
                    elif v == 90:
                        absolute = True
                    elif v == 91:
                        absolute = False
                    elif v == 20:
                        scale = 25.4
                    elif v == 21:
                        scale = 1.0
                    elif v == 4:
                        g4 = True
                elif letter == "F":
                    feed = max(v * scale, 1e-3)
                elif letter == "P" and g4:
                    dwell = v
                elif letter in "XYZ":
                    moved = True
                    val_mm = v * scale
                    if letter == "X":
                        nx = val_mm if absolute else x + val_mm
                    elif letter == "Y":
                        ny = val_mm if absolute else y + val_mm
                    else:
                        nz = val_mm if absolute else z + val_mm
            if moved and not line.startswith("$J"):
                d = ((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2) ** 0.5
                t += d / (rapid if motion == 0 else feed) * 60
                x, y, z = nx, ny, nz
            t += dwell
        cum.append(t)
    return len(cum), cum
