"""Small G-code helpers: bounding box of the burn area for framing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

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
