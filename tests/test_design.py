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
