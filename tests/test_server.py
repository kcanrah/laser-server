import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from laser_server.app import create_app
from laser_server.config import Settings
from laser_server.gcode import job_bounds
from laser_server.grbl import Grbl, clean_gcode_line

from .fake_grbl import FakeGrbl


class NoCamera:
    available = False
    error = "no camera in tests"

    def start(self): pass
    def shutdown(self): pass
    def snapshot(self): return None
    def mjpeg(self): return iter(())


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def fake():
    return FakeGrbl()


@pytest.fixture
def client(tmp_path, fake):
    settings = Settings()
    settings.jobs_dir = tmp_path / "jobs"
    grbl = Grbl("/dev/fake", serial_factory=lambda port, baud: fake, poll_interval=0.05)
    app = create_app(settings, grbl=grbl, camera=NoCamera())
    with TestClient(app) as c:
        assert wait_for(lambda: c.get("/api/status").json()["version"] is not None)
        assert wait_for(lambda: c.get("/api/status").json()["state"] == "Idle")
        yield c


def test_clean_line():
    assert clean_gcode_line("G1 X10 Y2.5 (comment) F1000 ; tail") == "G1X10Y2.5F1000"
    assert clean_gcode_line("; only comment") == ""


def test_bounds(tmp_path):
    p = tmp_path / "a.gcode"
    p.write_text("G21 G90\nG0 X5 Y5\nM4 S500\nG1 X25 Y5\nG1 X25 Y15\nG0 X100 Y100\nM5\n")
    assert job_bounds(p) == (5, 5, 25, 15)


def test_status_and_jog(client, fake):
    s = client.get("/api/status").json()
    assert s["connected"] and s["state"] == "Idle"
    assert client.post("/api/jog", json={"x": 10, "feed": 2000}).status_code == 200
    assert wait_for(lambda: any(l.startswith("$J=G91G21X10.000F2000") for l in fake.received_lines))


def test_upload_run_job_respects_buffer(client, fake):
    lines = "\n".join(f"G1 X{i % 50}.123 Y{i % 30}.456 S{i % 1000} F3000 ; move {i}" for i in range(400))
    r = client.post("/api/jobs", files={"file": ("test.gcode", lines.encode())})
    assert r.status_code == 200
    assert [f["name"] for f in client.get("/api/jobs").json()] == ["test.gcode"]

    assert client.post("/api/jobs/test.gcode/run").status_code == 200
    # Manual commands are refused while a job runs.
    assert client.post("/api/jog", json={"x": 1}).status_code == 409
    assert wait_for(lambda: client.get("/api/status").json()["job"]["result"] == "completed", 20)
    job = client.get("/api/status").json()["job"]
    assert job["lines_done"] == 400 and job["percent"] == 100
    assert not fake.overflowed
    assert fake.max_rx_used > 60  # buffer was actually being kept full
    assert len([l for l in fake.received_lines if l.startswith("G1")]) == 400


def test_stop_job(client, fake):
    fake.line_time = 0.02
    client.post("/api/jobs", files={"file": ("slow.nc", "\n".join(["G1X1F100"] * 500).encode())})
    client.post("/api/jobs/slow.nc/run")
    assert wait_for(lambda: client.get("/api/status").json()["job"]["lines_done"] > 5)
    assert client.post("/api/job/stop").status_code == 200
    s = client.get("/api/status").json()
    assert s["job"]["result"] == "stopped" and not s["job"]["running"]
    assert wait_for(lambda: client.get("/api/status").json()["state"] == "Idle")
    # Manual control works again after stopping.
    assert client.post("/api/jog", json={"x": 1}).status_code == 200


def test_job_error_halts(client, fake):
    fake.error_on.add("G1X3F100")
    client.post("/api/jobs", files={"file": ("bad.gcode", b"G1X1F100\nG1X2F100\nG1X3F100\nG1X4F100\n")})
    client.post("/api/jobs/bad.gcode/run")
    assert wait_for(lambda: (client.get("/api/status").json()["job"]["result"] or "").startswith("error"))


def test_frame_and_bad_names(client, fake):
    client.post("/api/jobs", files={"file": ("f.gcode", b"G0X10Y10\nG1X20Y10\nG1X20Y30\n")})
    r = client.post("/api/jobs/f.gcode/frame")
    assert r.json() == {"xmin": 10, "ymin": 10, "xmax": 20, "ymax": 30}
    assert wait_for(lambda: "$J=G90G21X10.000Y30.000F3000" in fake.received_lines)
    assert "M5" in fake.received_lines
    assert client.post("/api/jobs", files={"file": ("evil.sh", b"rm -rf /")}).status_code == 400
    assert client.get("/api/jobs/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)


def test_password(tmp_path, fake):
    settings = Settings()
    settings.jobs_dir = tmp_path / "jobs"
    settings.password = "secret"
    grbl = Grbl("/dev/fake", serial_factory=lambda p, b: fake)
    with TestClient(create_app(settings, grbl=grbl, camera=NoCamera())) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", auth=("me", "secret")).status_code == 200


def test_estimate_job(tmp_path):
    from laser_server.gcode import estimate_job

    p = tmp_path / "e.gcode"
    p.write_text("; comment\nG21 G90\nG0 X60 F9999\nG1 X60 Y60 F600 S100\nG1 X0 Y60\nG4 P2\n$H\n")
    n, cum = estimate_job(p, rapid_mm_min=6000)
    assert n == 6  # comment-only line dropped
    # 60mm rapid at 6000 = 0.6s; 2 x 60mm at 600 = 12s; dwell 2s
    assert cum[-1] == pytest.approx(0.6 + 12 + 2)


def test_running_job_reports_remaining(client, fake):
    fake.line_time = 0.01
    lines = "\n".join(f"G1 X{i % 2 * 10} F600" for i in range(300))  # 300 x 10mm at 600mm/min = 300s est
    client.post("/api/jobs", files={"file": ("est.gcode", lines.encode())})
    client.post("/api/jobs/est.gcode/run")
    assert wait_for(lambda: client.get("/api/status").json()["job"]["remaining"] is not None)
    j = client.get("/api/status").json()["job"]
    assert j["est_total"] == pytest.approx(299, abs=2) and 0 < j["remaining"] <= 300
    client.post("/api/job/stop")


def test_frame_with_laser(client, fake):
    client.post("/api/jobs", files={"file": ("f2.gcode", b"G0X10Y10\nG1X20Y10S500\nG1X20Y30\n")})
    assert client.post("/api/jobs/f2.gcode/frame?laser=true").status_code == 200
    assert wait_for(lambda: "M5S0" in fake.received_lines)
    rx = fake.received_lines
    i = rx.index("M3S10")  # 1% of S1000
    assert rx[i - 1] == "G90G21G0X10.000Y10.000"  # moved there with the laser off
    assert rx[i + 1:i + 5] == ["G1X20.000Y10.000F3000", "G1X20.000Y30.000F3000", "G1X10.000Y30.000F3000", "G1X10.000Y10.000F3000"]
    # default stays laser-off jogging
    fake.received_lines.clear()
    client.post("/api/jobs/f2.gcode/frame")
    assert wait_for(lambda: "$J=G90G21X10.000Y10.000F3000" in fake.received_lines)
    assert not any(l.startswith("M3") for l in fake.received_lines)
