"""Turn a browser design (JSON) into G-code.

Coordinates are millimetres in the work coordinate system, origin at the
bottom-left, Y up, which is what GRBL uses after "Set origin here".

Design format::

    {
      "name": "coaster",
      "layers": [
        {"id": "L1", "name": "Cut", "color": "#e5484d", "mode": "line",
         "power": 100, "speed": 300, "passes": 2, "enabled": true},
        {"id": "L2", "name": "Engrave", "color": "#3b82f6", "mode": "fill",
         "power": 30, "speed": 3000, "passes": 1, "hatch": 0.1, "angle": 0}
      ],
      "objects": [
        {"type": "rect", "layer": "L1", "x": 10, "y": 10, "w": 80, "h": 80, "rot": 0},
        {"type": "ellipse", "layer": "L2", "x": 20, "y": 20, "w": 60, "h": 60},
        {"type": "path", "layer": "L2", "x": 0, "y": 0, "w": 50, "h": 20,
         "paths": [[[x, y], ...], ...], "text": {...optional}}
      ]
    }

Every object maps its local geometry onto the box (x, y, w, h) and is then
rotated by `rot` degrees (counter-clockwise) about the box centre.
Layer modes: "line" traces outlines, "fill" hatches closed shapes,
"fill+line" does both (fill first).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

Point = tuple[float, float]
Polyline = list[Point]

DEFAULT_LAYER = {
    "mode": "line", "power": 50.0, "speed": 1000.0, "passes": 1,
    "hatch": 0.1, "angle": 0.0, "enabled": True, "overscan": 0.0,
}

MAX_SEGMENTS = 2_000_000  # guard against runaway fills on the Pi


class DesignError(ValueError):
    pass


# ---------------------------------------------------------------- geometry
def _ellipse_unit(n: int) -> Polyline:
    return [(0.5 + 0.5 * math.cos(2 * math.pi * i / n), 0.5 + 0.5 * math.sin(2 * math.pi * i / n))
            for i in range(n + 1)]


def bbox(polylines: Iterable[Polyline]) -> Optional[tuple[float, float, float, float]]:
    xs: list[float] = []
    ys: list[float] = []
    for pl in polylines:
        for x, y in pl:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def object_polylines(obj: dict) -> list[Polyline]:
    """Object geometry in work coordinates (mm)."""
    t = obj.get("type")
    w = float(obj.get("w", 0))
    h = float(obj.get("h", 0))
    x = float(obj.get("x", 0))
    y = float(obj.get("y", 0))
    rot = float(obj.get("rot", 0) or 0)

    if t == "rect":
        local = [[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]]
        lb = (0.0, 0.0, 1.0, 1.0)
    elif t == "ellipse":
        perimeter = math.pi * (abs(w) + abs(h)) / 2
        n = int(min(720, max(24, perimeter / 0.5)))
        local = [_ellipse_unit(n)]
        lb = (0.0, 0.0, 1.0, 1.0)
    elif t == "path":
        local = [[(float(p[0]), float(p[1])) for p in pl] for pl in obj.get("paths", []) if len(pl) >= 2]
        lb = bbox(local)
        if lb is None:
            return []
    else:
        raise DesignError(f"Unknown object type: {t!r}")

    lx0, ly0, lx1, ly1 = lb
    bw, bh = lx1 - lx0, ly1 - ly0
    sx = w / bw if bw > 1e-9 else 1.0
    sy = h / bh if bh > 1e-9 else 1.0
    cx, cy = x + w / 2, y + h / 2
    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))

    out = []
    for pl in local:
        pts = []
        for px, py in pl:
            ax = (px - lx0) * sx + x
            ay = (py - ly0) * sy + y
            if rot:
                dx, dy = ax - cx, ay - cy
                ax, ay = cx + dx * c - dy * s, cy + dx * s + dy * c
            pts.append((ax, ay))
        out.append(pts)
    return out


def _is_closed(pl: Polyline, tol: float = 1e-3) -> bool:
    return len(pl) > 2 and math.dist(pl[0], pl[-1]) <= tol


def hatch_fill(polylines: list[Polyline], spacing: float, angle_deg: float = 0.0) -> list[list[Point]]:
    """Scanline (even-odd) fill. Returns hatch rows; each row is a list of
    segments [(x0,y),(x1,y), ...] pairs in order, alternating direction."""
    if spacing <= 0:
        raise DesignError("Hatch spacing must be > 0")
    a = math.radians(angle_deg)
    c, s = math.cos(-a), math.sin(-a)
    edges = []
    for pl in polylines:
        if len(pl) < 3:
            continue
        pts = [(px * c - py * s, px * s + py * c) for px, py in pl]
        if not _is_closed(pts):
            pts.append(pts[0])
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if y0 != y1:
                edges.append((x0, y0, x1, y1))
    if not edges:
        return []
    ymin = min(min(e[1], e[3]) for e in edges)
    ymax = max(max(e[1], e[3]) for e in edges)
    nrows = int((ymax - ymin) / spacing)
    if nrows > 100_000:
        raise DesignError("Fill is too dense; increase the hatch spacing")

    # Bucket edges by the rows they cross so each row only checks nearby edges.
    buckets: list[list[tuple]] = [[] for _ in range(nrows + 1)]
    for e in edges:
        lo, hi = sorted((e[1], e[3]))
        i0 = max(0, math.ceil((lo - ymin - spacing / 2) / spacing))
        i1 = min(nrows, math.floor((hi - ymin - spacing / 2) / spacing))
        for i in range(i0, i1 + 1):
            buckets[i].append(e)

    ci, si = math.cos(a), math.sin(a)
    rows = []
    forward = True
    for i, bucket in enumerate(buckets):
        yy = ymin + spacing / 2 + i * spacing
        xs = []
        for x0, y0, x1, y1 in bucket:
            if (y0 <= yy < y1) or (y1 <= yy < y0):
                xs.append(x0 + (yy - y0) * (x1 - x0) / (y1 - y0))
        if len(xs) < 2:
            continue
        xs.sort()
        segs = [(xs[k], xs[k + 1]) for k in range(0, len(xs) - 1, 2) if xs[k + 1] - xs[k] > 1e-6]
        if not segs:
            continue
        if not forward:
            segs = [(b, a_) for a_, b in reversed(segs)]
        forward = not forward
        row = []
        for sx0, sx1 in segs:
            for px in (sx0, sx1):
                row.append((px * ci - yy * si, px * si + yy * ci))
        rows.append(row)
    return rows


def _order_paths(paths: list[Polyline], start: Point = (0.0, 0.0)) -> list[Polyline]:
    """Greedy nearest-neighbour ordering to cut travel; open paths may be reversed."""
    remaining = list(paths)
    ordered = []
    cur = start
    while remaining:
        best_i, best_d, best_rev = 0, float("inf"), False
        for i, pl in enumerate(remaining):
            d = math.dist(cur, pl[0])
            if d < best_d:
                best_i, best_d, best_rev = i, d, False
            if not _is_closed(pl):
                d = math.dist(cur, pl[-1])
                if d < best_d:
                    best_i, best_d, best_rev = i, d, True
        pl = remaining.pop(best_i)
        if best_rev:
            pl = pl[::-1]
        ordered.append(pl)
        cur = pl[-1]
    return ordered


# ----------------------------------------------------------------- G-code
@dataclass
class GcodeResult:
    gcode: str
    toolpaths: list = field(default_factory=list)  # [{"layer", "color", "paths": [...]}]
    bounds: Optional[tuple] = None
    cut_mm: float = 0.0
    travel_mm: float = 0.0
    est_seconds: float = 0.0
    lines: int = 0


class _Writer:
    def __init__(self, laser_mode: bool, rapid: float):
        self.out: list[str] = []
        self.pos: Point = (0.0, 0.0)
        self.f: Optional[float] = None
        self.s: Optional[float] = None
        self.laser_mode = laser_mode
        self.rapid = rapid
        self.cut_mm = 0.0
        self.travel_mm = 0.0
        self.seconds = 0.0
        self.on = False  # non-laser-mode spindle state

    @staticmethod
    def _n(v: float) -> str:
        t = f"{v:.3f}".rstrip("0").rstrip(".")
        return "0" if t in ("-0", "") else t

    def travel(self, p: Point) -> None:
        d = math.dist(self.pos, p)
        if d < 1e-4:
            return
        if not self.laser_mode and self.on:
            self.out.append("M5")
            self.on = False
        self.out.append(f"G0X{self._n(p[0])}Y{self._n(p[1])}")
        self.travel_mm += d
        self.seconds += d / self.rapid * 60
        self.pos = p

    def cut(self, p: Point, s: float, f: float) -> None:
        d = math.dist(self.pos, p)
        if d < 1e-4:
            return
        if not self.laser_mode and not self.on:
            self.out.append(f"M3S{self._n(s)}")
            self.on = True
            self.s = s
        line = f"G1X{self._n(p[0])}Y{self._n(p[1])}"
        if s != self.s:
            line += f"S{self._n(s)}"
            self.s = s
        if f != self.f:
            line += f"F{self._n(f)}"
            self.f = f
        self.out.append(line)
        if s > 0:
            self.cut_mm += d
        self.seconds += d / f * 60
        self.pos = p


def generate_gcode(design: dict, s_max: float = 1000.0, laser_mode: bool = True,
                   rapid_mm_min: float = 6000.0, return_home: bool = True) -> GcodeResult:
    layers = []
    for raw in design.get("layers", []):
        layer = {**DEFAULT_LAYER, **raw}
        if layer["mode"] not in ("line", "fill", "fill+line"):
            raise DesignError(f"Layer {layer.get('name')}: unknown mode {layer['mode']!r}")
        layer["power"] = min(100.0, max(0.0, float(layer["power"])))
        layer["speed"] = float(layer["speed"])
        layer["passes"] = int(layer["passes"])
        if layer["speed"] <= 0:
            raise DesignError(f"Layer {layer.get('name')}: speed must be > 0")
        if not 1 <= layer["passes"] <= 100:
            raise DesignError(f"Layer {layer.get('name')}: passes must be 1-100")
        layers.append(layer)

    by_layer: dict = {layer["id"]: [] for layer in layers}
    for obj in design.get("objects", []):
        if obj.get("layer") in by_layer:
            by_layer[obj["layer"]].append(obj)

    w = _Writer(laser_mode, rapid_mm_min)
    name = str(design.get("name") or "design").replace("\n", " ")
    w.out += [f"; {name} - generated by laser-server", "G90", "G21", "M5"]
    if laser_mode:
        w.out.append("M4S0")  # dynamic power: laser only fires while moving
    toolpaths = []
    all_pts: list[Polyline] = []
    segments = 0

    for layer in layers:
        objs = by_layer[layer["id"]]
        if not layer["enabled"] or not objs:
            continue
        s = round(s_max * layer["power"] / 100.0, 3)
        f = layer["speed"]
        mode = layer["mode"]
        outlines: list[Polyline] = []
        fills: list[list[Point]] = []
        for obj in objs:
            pls = object_polylines(obj)
            if "fill" in mode:
                fills.extend(hatch_fill(pls, float(layer["hatch"]), float(layer["angle"])))
            if "line" in mode:
                outlines.extend(pls)
        segments += sum(len(r) for r in fills) + sum(len(p) for p in outlines)
        if segments > MAX_SEGMENTS:
            raise DesignError("Design is too large to generate")

        w.out.append(f"; Layer {layer.get('name', layer['id'])}: {mode}, "
                     f"{layer['power']:g}% (S{s:g}), {f:g} mm/min, {layer['passes']} pass(es)")
        tp = {"layer": layer["id"], "color": layer.get("color", "#000"), "paths": []}
        for _ in range(layer["passes"]):
            for row in fills:
                w.travel(row[0])
                w.cut(row[1], s, f)
                for k in range(2, len(row), 2):
                    # Gap between segments on the same row: keep moving at
                    # cutting speed with power 0 (smoother than a G0).
                    if laser_mode:
                        w.cut(row[k], 0, f)
                    else:
                        w.travel(row[k])
                    w.cut(row[k + 1], s, f)
            for pl in _order_paths(outlines, w.pos):
                w.travel(pl[0])
                for p in pl[1:]:
                    w.cut(p, s, f)
        for row in fills:
            tp["paths"] += [[row[k], row[k + 1]] for k in range(0, len(row), 2)]
            all_pts += [[row[k], row[k + 1]] for k in range(0, len(row), 2)]
        tp["paths"] += outlines
        all_pts += outlines
        toolpaths.append(tp)

    w.out.append("M5")
    if return_home:
        w.travel((0.0, 0.0))
    w.out.append("M2")
    return GcodeResult(
        gcode="\n".join(w.out) + "\n",
        toolpaths=toolpaths,
        bounds=bbox(all_pts),
        cut_mm=round(w.cut_mm, 1),
        travel_mm=round(w.travel_mm, 1),
        est_seconds=round(w.seconds, 1),
        lines=len(w.out),
    )
