"""Image storage and processing for raster engraving."""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path
from typing import Optional

MAX_STORED_PX = 4000          # longest side kept on upload
MAX_RASTER_PX = 30_000_000    # pixels after resampling to the line interval


class ImageError(ValueError):
    pass


class ImageStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, image_id: str) -> Path:
        if not image_id or not all(c in "0123456789abcdef" for c in image_id):
            raise ImageError("Bad image id")
        return self.dir / f"{image_id}.png"

    def save(self, data: bytes) -> dict:
        """Store an upload as a greyscale PNG, keeping transparency (LA) if it has any.
        Transparent areas are never burned (see process())."""
        from PIL import Image, ImageOps

        try:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img)
            img.load()
        except Exception as e:  # noqa: BLE001
            raise ImageError(f"Could not read image: {e}") from e
        dpi = img.info.get("dpi")
        if img.mode in ("RGBA", "LA", "PA", "P") or "transparency" in img.info:
            img = img.convert("RGBA").convert("LA")
            if img.getchannel("A").getextrema()[0] == 255:
                img = img.convert("L")  # alpha channel present but fully opaque
        else:
            img = img.convert("L")
        if max(img.size) > MAX_STORED_PX:
            k = MAX_STORED_PX / max(img.size)
            if dpi:
                dpi = (dpi[0] * k, dpi[1] * k)
            img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        png = buf.getvalue()
        image_id = hashlib.sha1(png).hexdigest()[:20]
        self.path(image_id).write_bytes(png)
        result = {"id": image_id, "px_width": img.width, "px_height": img.height}
        try:
            if dpi and float(dpi[0]) > 1 and float(dpi[1]) > 1:
                result["mm_width"] = round(img.width / float(dpi[0]) * 25.4, 3)
                result["mm_height"] = round(img.height / float(dpi[1]) * 25.4, 3)
        except (TypeError, ValueError):
            pass
        return result

    def load(self, image_id: str):
        from PIL import Image

        p = self.path(image_id)
        if not p.is_file():
            raise ImageError(f"Image {image_id} is missing; re-import it")
        img = Image.open(p)
        img.load()
        return img.convert("LA") if img.mode in ("LA", "RGBA", "PA", "P") or "transparency" in img.info \
            else img.convert("L")


def process(img, obj: dict, layer: dict):
    """Resample, adjust and dither an image object for its layer.

    Returns (pixels, x0, y0, interval) where pixels is a uint8 numpy array
    (row 0 = top), 0 = full burn, 255 = no burn, and (x0, y0) is the
    bottom-left corner in mm of the (possibly rotated) raster."""
    import numpy as np
    from PIL import Image, ImageEnhance, ImageOps

    interval = float(layer.get("hatch", 0.1))
    if interval <= 0:
        raise ImageError("Line interval must be > 0")
    w, h = float(obj["w"]), float(obj["h"])
    if w <= 0 or h <= 0:
        raise ImageError("Image has no size")
    pw, ph = max(1, round(w / interval)), max(1, round(h / interval))
    if pw * ph > MAX_RASTER_PX:
        raise ImageError("Image is too large at this line interval; make it smaller or raise the interval")

    resample = Image.LANCZOS if pw * ph < img.width * img.height else Image.BICUBIC
    alpha = None
    if img.mode == "LA":
        a_full = img.getchannel("A")
        # Fully transparent pixels often hold black; make them white first so
        # resampling doesn't pull dark fringes into the edges.
        img = Image.composite(img.getchannel("L"), Image.new("L", img.size, 255), a_full)
        alpha = a_full.resize((pw, ph), resample)
    img = img.resize((pw, ph), resample)
    brightness = float(obj.get("brightness", 0) or 0)
    contrast = float(obj.get("contrast", 0) or 0)
    if brightness:
        img = ImageEnhance.Brightness(img).enhance(1 + brightness / 100)
    if contrast:
        img = ImageEnhance.Contrast(img).enhance(1 + contrast / 100)
    if obj.get("invert"):
        img = ImageOps.invert(img)
    if alpha is not None:
        # Transparent areas never burn, whatever brightness/contrast/invert did:
        # blend toward white by (1 - alpha).
        img = Image.composite(img, Image.new("L", img.size, 255), alpha)

    rot = float(obj.get("rot", 0) or 0)
    if rot:
        img = img.rotate(rot, resample=Image.BICUBIC, expand=True, fillcolor=255)

    dither = layer.get("dither", "floyd")
    if dither == "floyd":
        img = img.convert("1")  # Floyd-Steinberg
    elif dither == "threshold":
        img = img.point(lambda v: 255 if v >= 128 else 0)
    elif dither != "grayscale":
        raise ImageError(f"Unknown dither mode {dither!r}")
    pixels = np.asarray(img.convert("L"), dtype=np.uint8)

    cx, cy = float(obj["x"]) + w / 2, float(obj["y"]) + h / 2
    x0 = cx - pixels.shape[1] * interval / 2
    y0 = cy - pixels.shape[0] * interval / 2
    return pixels, x0, y0, interval


def power_rows(pixels, s_min: float, s_max: float, grayscale: bool):
    """Yield (row_index, starts, ends, powers) run-lengths per row; row 0 = top.
    Runs with power 0 are kept inside the row (laser off gaps)."""
    import numpy as np

    dark = (255 - pixels.astype(np.float32)) / 255.0
    if grayscale:
        power = np.where(dark > 0.004, s_min + (s_max - s_min) * dark, 0.0)
    else:
        power = np.where(dark > 0.5, s_max, 0.0)
    power = np.round(power).astype(np.int32)
    for r in range(power.shape[0]):
        row = power[r]
        nz = np.flatnonzero(row)
        if nz.size == 0:
            continue
        a, b = nz[0], nz[-1] + 1
        seg = row[a:b]
        change = np.flatnonzero(np.diff(seg)) + 1
        starts = np.concatenate(([0], change)) + a
        ends = np.concatenate((change, [len(seg)])) + a
        yield r, starts, ends, seg[starts - a]


def preview_data_url(pixels, color: str, max_px: int = 1600) -> str:
    """Burned pixels in the layer colour, unburned transparent, as a PNG data URL."""
    import numpy as np
    from PIL import Image

    img = Image.fromarray(pixels, "L")
    if max(img.size) > max_px:
        k = max_px / max(img.size)
        img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.BOX)
    alpha = Image.fromarray(255 - np.asarray(img), "L")
    c = color.lstrip("#")
    rgb = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) if len(c) == 6 else (0, 0, 0)
    out = Image.new("RGBA", img.size, rgb + (0,))
    out.putalpha(alpha)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=False, compress_level=6)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def placement(pixels, x0: float, y0: float, interval: float) -> dict:
    return {"x": round(x0, 3), "y": round(y0, 3),
            "w": round(pixels.shape[1] * interval, 3), "h": round(pixels.shape[0] * interval, 3)}


def burn_bounds(pixels, x0, y0, interval) -> Optional[tuple]:
    import numpy as np

    dark = pixels < 255
    rows = np.flatnonzero(dark.any(axis=1))
    cols = np.flatnonzero(dark.any(axis=0))
    if rows.size == 0:
        return None
    H = pixels.shape[0]
    return (float(x0 + cols[0] * interval), float(y0 + (H - rows[-1] - 1) * interval),
            float(x0 + (cols[-1] + 1) * interval), float(y0 + (H - rows[0]) * interval))


def estimate(pixels, interval: float, overscan: float, grayscale: bool):
    """Fast (no G-code) estimate: (burn_mm, scan_mm, rows) for one pass."""
    import numpy as np

    dark = pixels < (254 if grayscale else 128)
    rows = dark.any(axis=1)
    n = int(rows.sum())
    if not n:
        return 0.0, 0.0, 0
    d = dark[rows]
    first = d.argmax(axis=1)
    last = d.shape[1] - d[:, ::-1].argmax(axis=1)
    scan = float((last - first).sum()) * interval + n * 2 * overscan
    return float(dark.sum()) * interval, scan, n
