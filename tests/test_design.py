import math
import re

import pytest

from laser_server.design import DesignError, generate_gcode, hatch_fill, object_polylines
from laser_server.importers import default_font, svg_to_paths, text_to_paths

from .test_server import client, fake  # noqa: F401  (fixtures)

CUT = {"id": "c", "name": "Cut", "mode": "line", "power": 80, "speed": 400, "passes": 3}
FILL = {"id": "f", "name": "Fill", "mode": "fill", "power": 25, "speed": 3000, "hatch": 1.0, "angle": 0}


def moves(gcode):
    return [ln for ln in gcode.splitlines() if ln.startswith(("G0", "G1"))]


def test_rect_transform_and_rotation():
    pl = object_polylines({"type": "rect", "x": 10, "y": 20, "w": 30, "h": 10})[0]
    assert pl[0] == (10, 20) and pl[2] == (40, 30)
    pl = object_polylines({"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10, "rot": 90})[0]
    # 90 deg CCW about the centre (5,5): (0,0) -> (10,0)
    assert pl[0] == pytest.approx((10, 0))


def test_line_layer_passes_power_speed():
    d = {"layers": [CUT], "objects": [{"type": "rect", "layer": "c", "x": 0, "y": 0, "w": 10, "h": 5}]}
    r = generate_gcode(d, s_max=1000)
    g = r.gcode
    assert "M4S0" in g and g.rstrip().endswith("M2")
    assert "S800" in g and "F400" in g
    cuts = [m for m in moves(g) if m.startswith("G1")]
    assert len(cuts) == 4 * 3  # 4 edges, 3 passes
    assert r.cut_mm == pytest.approx(30 * 3)
    assert r.bounds == (0, 0, 10, 5)


def test_s_max_scaling():
    d = {"layers": [{**CUT, "power": 50}], "objects": [{"type": "rect", "layer": "c", "x": 0, "y": 0, "w": 1, "h": 1}]}
    assert "S127.5" in generate_gcode(d, s_max=255).gcode


def test_fill_hatches_inside_shape_only():
    rows = hatch_fill([[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]], 1.0)
    assert len(rows) == 10
    for row in rows:
        xs = sorted(p[0] for p in row)
        assert xs[0] == pytest.approx(0) and xs[-1] == pytest.approx(10)
    # A square with a square hole: middle rows have two segments
    outer = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    hole = [(3, 3), (7, 3), (7, 7), (3, 7), (3, 3)]
    rows = hatch_fill([outer, hole], 1.0)
    assert len(rows[5]) == 4


def test_fill_gcode_never_burns_outside():
    d = {"layers": [FILL], "objects": [{"type": "ellipse", "layer": "f", "x": 0, "y": 0, "w": 20, "h": 20}]}
    g = generate_gcode(d).gcode
    s = 0.0
    x = y = 0.0
    for line in g.splitlines():
        m = dict(re.findall(r"([XYS])([-\d.]+)", line))
        if "S" in m:
            s = float(m["S"])
        nx, ny = float(m.get("X", x)), float(m.get("Y", y))
        if line.startswith("G1") and s > 0:
            # burning endpoints must be within (a hair of) the circle
            assert math.hypot(nx - 10, ny - 10) <= 10.05
        x, y = nx, ny


def test_disabled_layer_and_validation():
    d = {"layers": [{**CUT, "enabled": False}], "objects": [{"type": "rect", "layer": "c", "x": 0, "y": 0, "w": 1, "h": 1}]}
    assert not [m for m in moves(generate_gcode(d, return_home=False).gcode) if m.startswith("G1")]
    with pytest.raises(DesignError):
        generate_gcode({"layers": [{**CUT, "speed": 0}], "objects": []})


def test_non_laser_mode_turns_off_for_travel():
    d = {"layers": [CUT], "objects": [
        {"type": "rect", "layer": "c", "x": 0, "y": 0, "w": 1, "h": 1},
        {"type": "rect", "layer": "c", "x": 50, "y": 50, "w": 1, "h": 1}]}
    lines = generate_gcode(d, laser_mode=False).gcode.splitlines()
    assert "M4S0" not in lines
    on = False
    for ln in lines:
        if ln.startswith("M3"):
            on = True
        elif ln == "M5":
            on = False
        elif ln.startswith("G0"):
            assert not on, "travel move with the laser on"


def test_text_and_svg_import():
    assert default_font()
    paths = text_to_paths("Hi", size_mm=10)
    assert paths and all(len(p) >= 2 for p in paths)
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="0 0 200 100">'
           b'<rect x="0" y="0" width="200" height="100"/></svg>')
    pls, w, h = svg_to_paths(svg)
    assert w == pytest.approx(20, abs=0.01) and h == pytest.approx(10, abs=0.01)


def test_design_api_roundtrip(client):  # noqa: F811
    design = {"name": "coaster", "layers": [CUT, FILL], "objects": [
        {"type": "ellipse", "layer": "c", "x": 0, "y": 0, "w": 90, "h": 90},
        {"type": "rect", "layer": "f", "x": 20, "y": 20, "w": 50, "h": 50}]}
    assert client.put("/api/designs/coaster", json=design).status_code == 200
    assert client.get("/api/designs").json()[0]["name"] == "coaster"
    assert client.get("/api/designs/coaster").json()["objects"] == design["objects"]

    prev = client.post("/api/design/preview", json=design).json()
    assert prev["est_seconds"] > 0 and len(prev["toolpaths"]) == 2

    r = client.post("/api/design/gcode", json={"design": design, "filename": "coaster"}).json()
    assert r["name"] == "coaster.gcode"
    assert "coaster.gcode" in [f["name"] for f in client.get("/api/jobs").json()]
    b = client.get("/api/jobs/coaster.gcode/bounds").json()
    assert b["xmin"] == pytest.approx(0, abs=0.01) and b["xmax"] == pytest.approx(90, abs=0.01)

    t = client.post("/api/text", json={"text": "A", "size": 5}).json()
    assert t["paths"]
    assert client.get("/api/fonts").json()["fonts"]
    bad = client.post("/api/design/preview", json={"layers": [{**CUT, "mode": "nope"}], "objects": []})
    assert bad.status_code == 422
    assert client.delete("/api/designs/coaster").status_code == 200


def test_machine_settings_from_grbl(client, fake):  # noqa: F811
    m = client.get("/api/machine").json()
    assert m["s_max"] == 1000 and m["bed_width"] == 400


def _png(draw):
    import io

    from PIL import Image, ImageDraw

    im = Image.new("RGB", (200, 100), "white")
    draw(ImageDraw.Draw(im))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


IMG_LAYER = {"id": "i", "name": "Img", "mode": "image", "power": 50, "min_power": 10, "speed": 6000,
             "hatch": 0.5, "dither": "threshold", "overscan": 2, "passes": 1}


def test_image_raster_gcode(tmp_path):
    from laser_server.images import ImageStore

    store = ImageStore(tmp_path)
    info = store.save(_png(lambda d: d.rectangle([50, 25, 149, 74], fill="black")))
    obj = {"type": "image", "image": info["id"], "layer": "i", "x": 0, "y": 0, "w": 100, "h": 50}
    r = generate_gcode({"layers": [IMG_LAYER], "objects": [obj]}, image_loader=store.load)
    # black box is the middle half of the image: x 25..75, y 12.5..37.5
    assert r.bounds == pytest.approx((25, 12.5, 75, 37.5), abs=0.01)
    burn_x = []
    s = 0.0
    x = 0.0
    for line in r.gcode.splitlines():
        m = dict(re.findall(r"([XYS])([-\d.]+)", line))
        if "S" in m:
            s = float(m["S"])
        nx = float(m.get("X", x))
        if line.startswith("G1") and s > 0:
            assert s == 500
            burn_x += [x, nx]
        x = nx
    assert min(burn_x) == pytest.approx(25) and max(burn_x) == pytest.approx(75)
    # rows alternate direction and overscan with laser off goes 2mm past the edge
    assert "G0X23Y" in r.gcode or "G0X77Y" in r.gcode
    # fast preview estimate is close to the real thing
    p = generate_gcode({"layers": [IMG_LAYER], "objects": [obj]}, image_loader=store.load, out=False, preview_images=True)
    assert p.est_seconds == pytest.approx(r.est_seconds, rel=0.1)
    assert p.toolpaths[0]["images"][0]["href"].startswith("data:image/png;base64,")


def test_image_grayscale_and_invert(tmp_path):
    from laser_server.images import ImageStore

    store = ImageStore(tmp_path)
    info = store.save(_png(lambda d: d.rectangle([0, 0, 99, 99], fill=(128, 128, 128))))
    obj = {"type": "image", "image": info["id"], "layer": "i", "x": 0, "y": 0, "w": 100, "h": 50}
    layer = {**IMG_LAYER, "dither": "grayscale"}
    g = generate_gcode({"layers": [layer], "objects": [obj]}, image_loader=store.load).gcode
    powers = {float(v) for v in re.findall(r"S([\d.]+)", g)} - {0.0}
    assert powers and all(100 < p < 500 for p in powers)  # mid grey between min and max
    plain = generate_gcode({"layers": [layer], "objects": [obj]}, image_loader=store.load, out=False)
    assert plain.bounds[2] == pytest.approx(50, abs=0.6)  # only the grey half burns
    inv = generate_gcode({"layers": [layer], "objects": [{**obj, "invert": True}]}, image_loader=store.load, out=False)
    assert inv.bounds[2] == pytest.approx(100, abs=0.6)  # inverted: the white half burns too


def test_image_layer_rules(tmp_path):
    with pytest.raises(DesignError):
        generate_gcode({"layers": [IMG_LAYER], "objects": [{"type": "rect", "layer": "i", "x": 0, "y": 0, "w": 1, "h": 1}]})
    with pytest.raises(DesignError):
        generate_gcode({"layers": [CUT], "objects": [{"type": "image", "image": "ab", "layer": "c", "x": 0, "y": 0, "w": 1, "h": 1}]},
                       image_loader=lambda i: None)


def test_image_api(client):  # noqa: F811
    r = client.post("/api/images", files={"file": ("box.png", _png(lambda d: d.rectangle([10, 10, 60, 60], fill="black")))})
    info = r.json()
    assert r.status_code == 200 and info["px_width"] == 200
    assert client.get(f"/api/images/{info['id']}").headers["content-type"] == "image/png"
    assert client.post("/api/images", files={"file": ("x.png", b"not an image")}).status_code == 422
    design = {"name": "pic", "layers": [IMG_LAYER], "objects": [
        {"type": "image", "image": info["id"], "layer": "i", "x": 0, "y": 0, "w": 40, "h": 20}]}
    prev = client.post("/api/design/preview", json=design).json()
    assert prev["toolpaths"][0]["images"] and prev["est_seconds"] > 0
    made = client.post("/api/design/gcode", json={"design": design}).json()
    assert made["name"] == "pic.gcode"
    b = client.get("/api/jobs/pic.gcode/bounds").json()
    assert b["xmin"] == pytest.approx(2, abs=0.5)  # frame covers only the burned box, not the overscan


def test_test_pattern(client):  # noqa: F811
    from laser_server.importers import text_to_paths
    from laser_server.testpattern import TestPattern, build

    p = TestPattern(power_min=20, power_max=80, power_steps=4, speed_min=10, speed_max=50, speed_steps=3,
                    cell=8, gap=2, x=5, y=5)
    d = build(p, text_to_paths)
    cells = [l for l in d["layers"] if l["id"].startswith("t")]
    assert len(cells) == 12
    assert sorted({l["power"] for l in cells}) == [20, 40, 60, 80]
    assert sorted({l["speed"] for l in cells}) == [10 * 60, 30 * 60, 50 * 60]  # stored as mm/min
    # top row is the slowest; columns go up in power left to right
    rects = {o["layer"]: o for o in d["objects"] if o["type"] == "rect"}
    assert rects["t0_0"]["y"] > rects["t2_0"]["y"] and rects["t0_0"]["x"] < rects["t0_3"]["x"]
    assert rects["t0_1"]["x"] - rects["t0_0"]["x"] == pytest.approx(10)
    # labels sit left of / above the grid, never below or left of the origin
    assert min(o["x"] for o in d["objects"]) == pytest.approx(5, abs=0.01)
    assert min(o["y"] for o in d["objects"]) == pytest.approx(5, abs=0.01)

    prev = client.post("/api/test-pattern/preview", json=p.model_dump() if hasattr(p, "model_dump") else p.dict()).json()
    assert prev["est_seconds"] > 0 and len(prev["toolpaths"]) == 13
    r = client.post("/api/test-pattern/gcode", json={"power_steps": 3, "speed_steps": 2, "labels": False}).json()
    assert r["name"] == "test-pattern.gcode"
    g = client.get("/api/jobs/test-pattern.gcode").text
    assert g.count("; Layer") == 6
    assert client.post("/api/test-pattern/preview", json={"power_min": 90, "power_max": 10}).status_code == 422
    assert client.post("/api/test-pattern/preview", json={"cell": 0}).status_code == 422


def test_presets_api(client, tmp_path):  # noqa: F811
    assert client.get("/api/presets").json() == []
    cut = {"material": "3mm plywood", "name": "Cut", "notes": "air assist on",
           "settings": {"mode": "line", "power": 100, "speed": 300, "passes": 2, "junk": 1}}
    r = client.post("/api/presets", json=cut)
    assert r.status_code == 200
    p = r.json()
    assert p["settings"] == {"mode": "line", "power": 100.0, "speed": 300.0, "passes": 2}  # unknown keys dropped
    eng = client.post("/api/presets", json={"material": "3mm plywood", "name": "Engrave",
                                             "settings": {"mode": "fill", "power": 30, "speed": 6000, "passes": 1, "hatch": 0.1}}).json()
    acr = client.post("/api/presets", json={"material": "Acrylic", "name": "Engrave",
                                             "settings": {"mode": "fill", "power": 20, "speed": 3000, "passes": 1}}).json()
    names = [(x["material"], x["name"]) for x in client.get("/api/presets").json()]
    assert names == [("3mm plywood", "Cut"), ("3mm plywood", "Engrave"), ("Acrylic", "Engrave")]  # sorted

    upd = client.put(f"/api/presets/{p['id']}", json={**cut, "settings": {**cut["settings"], "passes": 3}}).json()
    assert upd["id"] == p["id"] and upd["settings"]["passes"] == 3
    assert client.delete(f"/api/presets/{acr['id']}").status_code == 200
    assert client.delete(f"/api/presets/{acr['id']}").status_code == 422

    # validation
    for bad in ({"name": "", "settings": {"mode": "line", "power": 1, "speed": 1, "passes": 1}},
                {"name": "x", "settings": {"mode": "laser", "power": 1, "speed": 1, "passes": 1}},
                {"name": "x", "settings": {"mode": "line", "power": 1, "speed": 0, "passes": 1}}):
        assert client.post("/api/presets", json=bad).status_code == 422

    # export -> import into an empty store merges by material+name
    exported = client.get("/api/presets/export")
    assert "attachment" in exported.headers["content-disposition"]
    client.delete(f"/api/presets/{eng['id']}")
    r = client.post("/api/presets/import", files={"file": ("p.json", exported.content)})
    assert r.json()["imported"] == 2
    after = client.get("/api/presets").json()
    assert len(after) == 2 and {x["name"] for x in after} == {"Cut", "Engrave"}
    assert client.post("/api/presets/import", files={"file": ("p.json", b"nope")}).status_code == 422


def test_negative_fill_engraves_background():
    from laser_server.design import negative_border

    layer = {**FILL, "negative": True, "margin": 5, "hatch": 0.5}
    d = {"layers": [layer], "objects": [{"type": "rect", "layer": "f", "x": 10, "y": 10, "w": 20, "h": 10}]}
    r = generate_gcode(d)
    # burns out to the margin...
    assert r.bounds[0] == pytest.approx(5) and r.bounds[2] == pytest.approx(35)
    # ...but never inside the shape (10..30 x 10..20)
    s, x, y = 0.0, 0.0, 0.0
    for line in r.gcode.splitlines():
        m = dict(re.findall(r"([XYS])([-\d.]+)", line))
        if "S" in m:
            s = float(m["S"])
        nx, ny = float(m.get("X", x)), float(m.get("Y", y))
        if line.startswith("G1") and s > 0 and 10 < ny < 20:
            # a burning move on a row through the shape stays left or right of it
            assert max(x, nx) <= 10 + 1e-6 or min(x, nx) >= 30 - 1e-6
        x, y = nx, ny
    # text-like holes: a shape with a hole keeps the hole burned (even-odd)
    outer = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    hole = [(3, 3), (7, 3), (7, 7), (3, 7), (3, 3)]
    rows = hatch_fill([negative_border([outer, hole], 2), outer, hole], 1.0)
    mid = [r_ for r_ in rows if abs(r_[0][1] - 5.5) < 1e-6][0]
    xs = sorted(p[0] for p in mid)
    assert xs == pytest.approx([-2, 0, 3, 7, 10, 12])  # border, hole, border burn; shape body left
    # normal (non-negative) fill unchanged
    plain = generate_gcode({**d, "layers": [{**FILL, "hatch": 0.5}]})
    assert plain.bounds[0] == pytest.approx(10)


def test_transparent_png_never_burns(tmp_path, ):
    import io

    from PIL import Image, ImageDraw

    from laser_server.images import ImageStore

    # transparent canvas (stored as transparent *black*, like many exports) with a black square
    im = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([50, 25, 149, 74], fill=(0, 0, 0, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    store = ImageStore(tmp_path)
    info = store.save(buf.getvalue())
    assert Image.open(store.path(info["id"])).mode == "LA"  # transparency kept
    obj = {"type": "image", "image": info["id"], "layer": "i", "x": 0, "y": 0, "w": 100, "h": 50}
    for extra in ({}, {"invert": True}, {"brightness": -100}):
        r = generate_gcode({"layers": [IMG_LAYER], "objects": [{**obj, **extra}]}, image_loader=store.load, out=False)
        if extra.get("invert"):
            # inverted: the opaque square becomes white (nothing to burn), the transparent area still never burns
            assert r.bounds is None
        else:
            assert r.bounds == pytest.approx((25, 12.5, 75, 37.5), abs=0.6)
    # an opaque PNG is still stored without an alpha channel
    buf2 = io.BytesIO()
    Image.new("RGBA", (10, 10), (255, 0, 0, 255)).save(buf2, "PNG")
    assert Image.open(store.path(store.save(buf2.getvalue())["id"])).mode == "L"
