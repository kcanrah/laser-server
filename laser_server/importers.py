"""Convert text (via system fonts) and SVG files into polylines in mm, Y up."""

from __future__ import annotations

import glob
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .design import Polyline, bbox

FONT_DIRS = [
    "/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".fonts"),
    str(Path.home() / ".local/share/fonts"), "/Library/Fonts", "/System/Library/Fonts",
]
PREFERRED_DEFAULTS = ["DejaVuSans", "DejaVu Sans", "LiberationSans", "Arial", "OpenSans", "Helvetica"]

TOLERANCE_MM = 0.05  # max chord error when flattening curves


class ImportError_(ValueError):
    pass


# ------------------------------------------------------------------- fonts
@lru_cache(maxsize=1)
def list_fonts() -> dict[str, str]:
    """Map a display name ("DejaVu Sans Bold") to a font file path."""
    from fontTools.ttLib import TTFont

    dirs = [d for d in os.environ.get("LASER_FONT_DIRS", "").split(":") if d] + FONT_DIRS
    fonts: dict[str, str] = {}
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "**", "*.[ot]tf"), recursive=True)):
            try:
                tt = TTFont(path, lazy=True, fontNumber=0)
                name = tt["name"].getBestFullName() or Path(path).stem
                if "glyf" not in tt and "CFF " not in tt:
                    continue
                tt.close()
            except Exception:
                continue
            fonts.setdefault(str(name), path)
    return dict(sorted(fonts.items(), key=lambda kv: kv[0].lower()))


def default_font() -> Optional[str]:
    fonts = list_fonts()
    for pref in PREFERRED_DEFAULTS:
        for name in fonts:
            if name.replace(" ", "").lower() == pref.replace(" ", "").lower():
                return name
    return next(iter(fonts), None)


def _flatten_quad(p0, p1, p2, scale):
    length = (math.dist(p0, p1) + math.dist(p1, p2)) * scale
    n = max(2, min(64, int(math.sqrt(length / TOLERANCE_MM)) + 1))
    pts = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        pts.append((mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                    mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]))
    return pts


def _flatten_cubic(p0, p1, p2, p3, scale):
    length = (math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)) * scale
    n = max(2, min(96, int(math.sqrt(length / TOLERANCE_MM)) + 1))
    pts = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        pts.append(tuple(mt ** 3 * a + 3 * mt * mt * t * b + 3 * mt * t * t * c + t ** 3 * d
                         for a, b, c, d in zip(p0, p1, p2, p3)))
    return pts


def text_to_paths(text: str, font: Optional[str] = None, size_mm: float = 10.0,
                  line_spacing: float = 1.2, align: str = "left") -> list[Polyline]:
    """Outline `text` with a system font. `size_mm` is the em size; the first
    line's baseline is at y=0 and further lines go downwards."""
    from fontTools.pens.basePen import BasePen
    from fontTools.ttLib import TTFont

    fonts = list_fonts()
    font = font or default_font()
    if not font or font not in fonts:
        raise ImportError_(f"Font not found: {font!r}. Install fonts-dejavu-core on the Pi.")
    tt = _load_font(fonts[font])
    glyphset = tt.getGlyphSet()
    cmap = tt.getBestCmap() or {}
    upem = tt["head"].unitsPerEm
    scale = size_mm / upem
    hmtx = tt["hmtx"]

    class Pen(BasePen):
        def __init__(self):
            super().__init__(glyphset)
            self.paths: list[list] = []
            self.cur: list = []

        def _moveTo(self, p):
            self._flush()
            self.cur = [p]

        def _lineTo(self, p):
            self.cur.append(p)

        def _qCurveToOne(self, p1, p2):
            self.cur += _flatten_quad(self.cur[-1], p1, p2, scale)

        def _curveToOne(self, p1, p2, p3):
            self.cur += _flatten_cubic(self.cur[-1], p1, p2, p3, scale)

        def _closePath(self):
            if self.cur and self.cur[0] != self.cur[-1]:
                self.cur.append(self.cur[0])
            self._flush()

        _endPath = _closePath

        def _flush(self):
            if len(self.cur) >= 2:
                self.paths.append(self.cur)
            self.cur = []

    lines = text.split("\n")
    widths = []
    for line in lines:
        widths.append(sum(hmtx[cmap.get(ord(ch), ".notdef")][0] for ch in line) * scale)
    max_w = max(widths) if widths else 0

    out: list[Polyline] = []
    for li, line in enumerate(lines):
        x = 0.0
        if align == "center":
            x = (max_w - widths[li]) / 2
        elif align == "right":
            x = max_w - widths[li]
        y = -li * size_mm * line_spacing
        for ch in line:
            gname = cmap.get(ord(ch), ".notdef")
            pen = Pen()
            glyphset[gname].draw(pen)
            pen._flush()
            for pl in pen.paths:
                out.append([(x + px * scale, y + py * scale) for px, py in pl])
            x += hmtx[gname][0] * scale
    return out


@lru_cache(maxsize=8)
def _load_font(path: str):
    from fontTools.ttLib import TTFont

    return TTFont(path, fontNumber=0)


# --------------------------------------------------------------------- SVG
def svg_to_paths(data: bytes) -> tuple[list[Polyline], float, float]:
    """Parse an SVG into polylines (mm, Y up, origin at the drawing's
    bottom-left). Uses the document's physical size if it has one (mm, in),
    otherwise assumes 96 px per inch like Inkscape and browsers."""
    import io

    from svgelements import SVG, Close, Line, Move, Path as SvgPath, Shape

    try:
        svg = SVG.parse(io.BytesIO(data), ppi=96.0, reify=True)
    except Exception as e:  # noqa: BLE001
        raise ImportError_(f"Could not read SVG: {e}") from e

    px_to_mm = 25.4 / 96.0
    polylines: list[Polyline] = []
    for el in svg.elements():
        if not isinstance(el, Shape):
            continue
        if el.values.get("visibility") == "hidden" or el.values.get("display") == "none":
            continue
        try:
            path = abs(SvgPath(el))
        except Exception:
            continue
        cur: list = []
        for seg in path.segments():
            if isinstance(seg, Move):
                if len(cur) >= 2:
                    polylines.append(cur)
                cur = [(seg.end.x, seg.end.y)]
            elif isinstance(seg, (Line, Close)):
                if seg.end is not None:
                    if not cur:
                        cur = [(seg.start.x, seg.start.y)]
                    cur.append((seg.end.x, seg.end.y))
            else:
                if not cur:
                    cur = [(seg.start.x, seg.start.y)]
                try:
                    length_mm = seg.length(error=1e-3) * px_to_mm
                except Exception:
                    length_mm = 10.0
                n = max(2, min(2000, int(length_mm / 0.2) + 1))
                for i in range(1, n + 1):
                    p = seg.point(i / n)
                    cur.append((p.x, p.y))
        if len(cur) >= 2:
            polylines.append(cur)

    if not polylines:
        raise ImportError_("No drawable shapes found in the SVG")

    # px -> mm and flip Y so it's Y-up with the drawing's bottom-left at 0,0.
    pls = [[(x * px_to_mm, -y * px_to_mm) for x, y in pl] for pl in polylines]
    x0, y0, x1, y1 = bbox(pls)
    pls = [[(round(x - x0, 4), round(y - y0, 4)) for x, y in pl] for pl in pls]
    return pls, round(x1 - x0, 4), round(y1 - y0, 4)
