"""Material test pattern: a grid of squares, power varying across columns
and speed varying down rows, with engraved labels. Built as an ordinary
design (one layer per cell) so it goes through the same G-code generator."""

from __future__ import annotations

from typing import Callable, Optional

from pydantic import BaseModel, Field

from .design import DesignError, bbox


class TestPattern(BaseModel):
    mode: str = "fill"  # fill | line | fill+line
    power_min: float = Field(10, ge=0, le=100)
    power_max: float = Field(100, ge=0, le=100)
    power_steps: int = Field(5, ge=1, le=20)
    speed_min: float = Field(10, gt=0, le=1000)   # mm/s
    speed_max: float = Field(100, gt=0, le=1000)  # mm/s
    speed_steps: int = Field(5, ge=1, le=20)
    cell: float = Field(10, ge=1, le=200)          # square size, mm
    gap: float = Field(2, ge=0, le=50)             # between squares, mm
    passes: int = Field(1, ge=1, le=20)
    interval: float = Field(0.1, ge=0.02, le=5)    # fill line interval, mm
    x: float = 10                                  # bottom-left of the whole pattern, mm
    y: float = 10
    labels: bool = True
    label_power: float = Field(20, ge=0, le=100)
    label_speed: float = Field(40, gt=0, le=1000)  # mm/s
    label_size: Optional[float] = Field(None, gt=0, le=50)  # mm; default from cell size


def _steps(lo: float, hi: float, n: int) -> list[float]:
    if n == 1:
        return [round(lo, 2)]
    return [round(lo + (hi - lo) * i / (n - 1), 2) for i in range(n)]


def _fmt(v: float) -> str:
    return f"{v:g}" if v == int(v) or abs(v) >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")


def _hue_color(i: int, n: int) -> str:
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(0.0 + 0.75 * (i / max(n - 1, 1)), 0.75, 0.95)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def build(p: TestPattern, text_fn: Callable[..., list]) -> dict:
    """Return a design dict for the test pattern."""
    if p.mode not in ("fill", "line", "fill+line"):
        raise DesignError("Mode must be fill, line or fill+line")
    if p.power_min > p.power_max:
        raise DesignError("Min power must be ≤ max power")
    if p.speed_min > p.speed_max:
        raise DesignError("Min speed must be ≤ max speed")
    powers = _steps(p.power_min, p.power_max, p.power_steps)
    speeds = _steps(p.speed_min, p.speed_max, p.speed_steps)
    cols, rows = len(powers), len(speeds)
    pitch = p.cell + p.gap
    grid_w = cols * p.cell + (cols - 1) * p.gap
    grid_h = rows * p.cell + (rows - 1) * p.gap

    layers: list[dict] = []
    objects: list[dict] = []
    label_objs: list[tuple] = []  # (paths, anchor, kind)

    size = p.label_size or max(1.5, min(5.0, p.cell * 0.35))
    margin_left = 0.0
    if p.labels:
        def text(s: str) -> list:
            paths = text_fn(s, None, size)
            if not paths:
                raise DesignError("Could not render label text (no fonts installed?)")
            return paths

        col_labels = [text(_fmt(v)) for v in powers]
        row_labels = [text(_fmt(v)) for v in speeds]
        power_title = text("Power %")
        speed_title = text("mm/s")
        wmax = max((bbox(t)[2] - bbox(t)[0]) for t in row_labels + [speed_title])
        margin_left = wmax + size * 0.8

    gx = p.x + margin_left                  # grid bottom-left
    gy = p.y
    top = gy + grid_h                       # grid top edge

    n = rows * cols
    k = 0
    for r, speed in enumerate(speeds):      # row 0 (top) = slowest
        for c, power in enumerate(powers):
            lid = f"t{r}_{c}"
            layers.append({
                "id": lid, "name": f"{_fmt(power)}% @ {_fmt(speed)} mm/s", "color": _hue_color(k, n),
                "mode": p.mode, "power": power, "speed": speed * 60, "passes": p.passes,
                "hatch": p.interval, "angle": 0, "enabled": True,
            })
            objects.append({"id": f"o{r}_{c}", "type": "rect", "layer": lid,
                            "x": round(gx + c * pitch, 3), "y": round(top - p.cell - r * pitch, 3),
                            "w": p.cell, "h": p.cell, "rot": 0})
            k += 1

    if p.labels:
        layers.append({"id": "labels", "name": "Labels", "color": "#9ca3af", "mode": "line",
                       "power": p.label_power, "speed": p.label_speed * 60, "passes": 1,
                       "hatch": 0.1, "angle": 0, "enabled": True})

        def place(paths, x, y, align="left"):
            b = bbox(paths)
            w, h = b[2] - b[0], b[3] - b[1]
            if align == "center":
                x -= w / 2
            elif align == "right":
                x -= w
            objects.append({"id": f"l{len(objects)}", "type": "path", "layer": "labels",
                            "x": round(x, 3), "y": round(y, 3), "w": round(w, 3), "h": round(h, 3),
                            "rot": 0, "paths": paths})

        row1 = top + size * 0.6                                  # column values
        row2 = row1 + size * 1.5                                  # "Power %" title
        for c, paths in enumerate(col_labels):
            place(paths, gx + c * pitch + p.cell / 2, row1, "center")
        place(power_title, gx + grid_w / 2, row2, "center")
        for r, paths in enumerate(row_labels):
            yc = top - p.cell / 2 - r * pitch
            place(paths, gx - size * 0.8, yc - size * 0.35, "right")
        place(speed_title, gx - size * 0.8, row1, "right")

    x0 = min(o["x"] for o in objects)
    y0 = min(o["y"] for o in objects)
    x1 = max(o["x"] + o["w"] for o in objects)
    y1 = max(o["y"] + o["h"] for o in objects)
    return {"name": "test-pattern", "layers": layers, "objects": objects,
            "size": [round(x1 - x0, 2), round(y1 - y0, 2)],
            "extent": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)]}
