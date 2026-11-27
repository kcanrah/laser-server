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
"fill+line" does both (fill first), "image" raster-engraves image objects
({"type": "image", "image": "<id>", ..., "invert", "brightness", "contrast"}).
Speeds are mm/min (the UI shows mm/s).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

Point = tuple[float, float]
Polyline = list[Point]

DEFAULT_LAYER = {
    "mode": "line", "power": 50.0, "speed": 1000.0, "passes": 1,
    "hatch": 0.1, "angle": 0.0, "enabled": True,
    # image layers
    "min_power": 0.0, "dither": "floyd", "overscan": 2.0, "bidirectional": True,
    # fill layers: engrave the background (a border around the shapes) instead of the shapes
    "negative": False, "margin": 2.0,
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


def negative_border(shapes: list[Polyline], margin: float) -> Optional[Polyline]:
    """Closed rectangle `margin` mm outside the bounding box of `shapes`."""
    b = bbox(shapes)
    if b is None:
        return None
    x0, y0, x1, y1 = b[0] - margin, b[1] - margin, b[2] + margin, b[3] + margin
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


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
    gcode: Optional[str] = None  # only when no output file was given
    toolpaths: list = field(default_factory=list)  # [{"layer", "color", "paths", "images"}]
    bounds: Optional[tuple] = None
    cut_mm: float = 0.0
    travel_mm: float = 0.0
    est_seconds: float = 0.0
    lines: int = 0


class _NullSink:
    def write(self, _s: str) -> None:
        pass


class _Writer:
    def __init__(self, sink, laser_mode: bool, rapid: float):
        self.sink = sink
        self.lines = 0
        self.pos: Optional[Point] = None  # unknown until the first move
        self.f: Optional[float] = None
        self.s: Optional[float] = None
        self.laser_mode = laser_mode
        self.rapid = rapid
        self.cut_mm = 0.0
        self.travel_mm = 0.0
        self.seconds = 0.0
        self.on = False  # spindle state when not in laser mode

    def emit(self, line: str) -> None:
        self.sink.write(line + "\n")
        self.lines += 1
        if self.lines > MAX_LINES:
            raise DesignError("Design produces too much G-code; make it smaller or coarser")

    @staticmethod
    def _n(v: float) -> str:
        t = f"{v:.3f}".rstrip("0").rstrip(".")
        return "0" if t in ("-0", "") else t

    def _xy(self, p: Point) -> str:
        if self.pos is None:
            return f"X{self._n(p[0])}Y{self._n(p[1])}"
        out = ""
        if self._n(p[0]) != self._n(self.pos[0]):
            out += f"X{self._n(p[0])}"
        if self._n(p[1]) != self._n(self.pos[1]):
            out += f"Y{self._n(p[1])}"
        return out

    def travel(self, p: Point) -> None:
        d = math.dist(self.pos, p) if self.pos is not None else 0.0
        if self.pos is not None and d < 1e-4:
            return
        if not self.laser_mode and self.on:
            self.emit("M5")
            self.on = False
        self.emit(f"G0{self._xy(p)}")
        self.travel_mm += d
        self.seconds += d / self.rapid * 60
        self.pos = p

    def cut(self, p: Point, s: float, f: float) -> None:
        if self.pos is None:
            self.travel(p)
            return
        d = math.dist(self.pos, p)
        if d < 1e-4:
            return
        if not self.laser_mode and not self.on:
            self.emit(f"M3S{self._n(s)}")
            self.on = True
            self.s = s
        line = f"G1{self._xy(p)}"
        if s != self.s:
            line += f"S{self._n(s)}"
            self.s = s
        if f != self.f:
            line += f"F{self._n(f)}"
            self.f = f
        self.emit(line)
        if s > 0:
            self.cut_mm += d
        self.seconds += d / f * 60
        self.pos = p


MAX_LINES = 12_000_000

LAYER_MODES = ("line", "fill", "fill+line", "image")


def _validate_layer(raw: dict) -> dict:
    layer = {**DEFAULT_LAYER, **raw}
    name = layer.get("name", layer.get("id"))
    if layer["mode"] not in LAYER_MODES:
        raise DesignError(f"Layer {name}: unknown mode {layer['mode']!r}")
    layer["power"] = min(100.0, max(0.0, float(layer["power"])))
    layer["min_power"] = min(layer["power"], max(0.0, float(layer.get("min_power", 0) or 0)))
    layer["speed"] = float(layer["speed"])
    layer["passes"] = int(layer["passes"])
    layer["overscan"] = max(0.0, float(layer.get("overscan", 0) or 0))
    layer["margin"] = float(layer.get("margin", 0) or 0)
    if not 0 <= layer["margin"] <= 500:
        raise DesignError(f"Layer {name}: negative margin must be 0-500 mm")
    if layer["speed"] <= 0:
        raise DesignError(f"Layer {name}: speed must be > 0")
    if not 1 <= layer["passes"] <= 100:
        raise DesignError(f"Layer {name}: passes must be 1-100")
    if float(layer["hatch"]) <= 0:
        raise DesignError(f"Layer {name}: line interval must be > 0")
    return layer


def generate_gcode(design: dict, s_max: float = 1000.0, laser_mode: bool = True,
                   rapid_mm_min: float = 6000.0, return_home: bool = True,
                   out=None, image_loader=None, preview_images: bool = False) -> GcodeResult:
    """Generate G-code for a design. Writes to `out` (a text file) if given,
    otherwise returns the text in result.gcode. Pass out=False to only compute
    stats and toolpaths (for previews). `image_loader(id)` returns a PIL image."""
    layers = [_validate_layer(raw) for raw in design.get("layers", [])]

    by_layer: dict = {layer["id"]: [] for layer in layers}
    for obj in design.get("objects", []):
        if obj.get("layer") in by_layer:
            by_layer[obj["layer"]].append(obj)

    buf = None
    if out is None:
        import io
        buf = out = io.StringIO()
    elif out is False:
        out = _NullSink()
    w = _Writer(out, laser_mode, rapid_mm_min)
    name = str(design.get("name") or "design").replace("\n", " ")
    for line in (f"; {name} - generated by laser-server", "G90", "G21", "M5"):
        w.emit(line)
    if laser_mode:
        w.emit("M4S0")  # dynamic power: laser only fires while moving
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
        lname = layer.get("name", layer["id"])
        tp = {"layer": layer["id"], "color": layer.get("color", "#000"), "paths": [], "images": []}

        if mode == "image":
            for obj in objs:
                if obj.get("type") != "image":
                    raise DesignError(f"Layer {lname} is an image layer; move shapes and text to a line or fill layer")
            w.emit(f"; Layer {lname}: image ({layer.get('dither', 'floyd')}), {layer['min_power']:g}-"
                   f"{layer['power']:g}%, {f:g} mm/min, interval {layer['hatch']:g} mm, {layer['passes']} pass(es)")
            for obj in objs:
                if image_loader is None:
                    raise DesignError("Images are not available")
                from .images import burn_bounds, placement, preview_data_url, process
                pixels, x0, y0, interval = process(image_loader(obj["image"]), obj, layer)
                bb = burn_bounds(pixels, x0, y0, interval)
                if bb is None:
                    continue
                all_pts.append([(bb[0], bb[1]), (bb[2], bb[3])])
                if preview_images:
                    tp["images"].append({**placement(pixels, x0, y0, interval),
                                         "href": preview_data_url(pixels, tp["color"])})
                s_lo = round(s_max * layer["min_power"] / 100.0, 3)
                for _ in range(layer["passes"]):
                    _raster(w, pixels, x0, y0, interval, s_lo, s, f, layer)
            toolpaths.append(tp)
            continue

        outlines: list[Polyline] = []
        fills: list[list[Point]] = []
        for obj in objs:
            if obj.get("type") == "image":
                raise DesignError(f"Images must be on an image layer (layer {lname} is {mode})")
        negative = bool(layer.get("negative")) and "fill" in mode
        if negative:
            # Fill a rectangle around everything on the layer, with the shapes as
            # holes (even-odd), so the shapes are left standing and the rest burns.
            shapes = [pl for obj in objs for pl in object_polylines(obj)]
            border = negative_border(shapes, float(layer.get("margin", 0) or 0))
            if border:
                fills = hatch_fill([border] + shapes, float(layer["hatch"]), float(layer["angle"]))
                if "line" in mode:
                    outlines = shapes + [border]
        else:
            for obj in objs:
                pls = object_polylines(obj)
                if "fill" in mode:
                    fills.extend(hatch_fill(pls, float(layer["hatch"]), float(layer["angle"])))
                if "line" in mode:
                    outlines.extend(pls)
        segments += sum(len(r) for r in fills) + sum(len(p) for p in outlines)
        if segments > MAX_SEGMENTS:
            raise DesignError("Design is too large to generate")

        w.emit(f"; Layer {lname}: {mode}{' (negative)' if negative else ''}, {layer['power']:g}% (S{s:g}), "
               f"{f:g} mm/min, {layer['passes']} pass(es)")
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
            for pl in _order_paths(outlines, w.pos or (0.0, 0.0)):
                w.travel(pl[0])
                for p in pl[1:]:
                    w.cut(p, s, f)
        for row in fills:
            segs = [[row[k], row[k + 1]] for k in range(0, len(row), 2)]
            tp["paths"] += segs
            all_pts += segs
        tp["paths"] += outlines
        all_pts += outlines
        toolpaths.append(tp)

    w.emit("M5")
    if return_home:
        w.travel((0.0, 0.0))
    w.emit("M2")
    return GcodeResult(
        gcode=buf.getvalue() if buf is not None else None,
        toolpaths=toolpaths,
        bounds=bbox(all_pts),
        cut_mm=round(w.cut_mm, 1),
        travel_mm=round(w.travel_mm, 1),
        est_seconds=round(w.seconds, 1),
        lines=w.lines,
    )


def _raster(w: _Writer, pixels, x0: float, y0: float, interval: float,
            s_lo: float, s_hi: float, f: float, layer: dict) -> None:
    """Engrave a processed image row by row (top to bottom)."""
    from .images import power_rows

    grayscale = layer.get("dither") == "grayscale"
    bidir = layer.get("bidirectional", True)
    ov = float(layer.get("overscan", 0) or 0)
    if isinstance(w.sink, _NullSink):
        # Preview: estimate time without generating every move (slow on a Pi).
        from .images import estimate

        burn_mm, scan_mm, nrows = estimate(pixels, interval, ov if w.laser_mode else 0, grayscale)
        w.cut_mm += burn_mm
        w.seconds += scan_mm / f * 60 + nrows * (interval if bidir else 2 * scan_mm / max(nrows, 1)) / w.rapid * 60
        return
    H = pixels.shape[0]
    forward = True
    for r, starts, ends, powers in power_rows(pixels, s_lo, s_hi, grayscale):
        y = y0 + (H - r - 0.5) * interval
        runs = list(zip(starts.tolist(), ends.tolist(), powers.tolist()))
        if forward:
            xs = [(x0 + a * interval, x0 + b * interval, p) for a, b, p in runs]
            sign = 1
        else:
            xs = [(x0 + b * interval, x0 + a * interval, p) for a, b, p in reversed(runs)]
            sign = -1
        start, end = xs[0][0], xs[-1][1]
        if ov and w.laser_mode:
            w.travel((start - sign * ov, y))
            w.cut((start, y), 0, f)
        else:
            w.travel((start, y))
        for _a, b, p in xs:
            w.cut((b, y), p, f)
        if ov and w.laser_mode:
            w.cut((end + sign * ov, y), 0, f)
        if bidir:
            forward = not forward
