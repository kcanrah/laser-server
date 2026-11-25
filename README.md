# laser-server

A small Python web server for running a GRBL laser engraver from a Raspberry Pi, with a live view from a USB webcam.

- **Live camera**: MJPEG stream of the bed, plus snapshots
- **Jobs**: upload G-code (from LightBurn, LaserGRBL, Inkscape, etc.), frame it with the laser off, then run, pause, resume, or stop it, with a progress readout
- **Machine control**: home, unlock, jog (buttons or arrow keys), set and go to the work origin, live position and state
- **Design**: draw rectangles, ellipses, lines and text, or import SVGs, right in the browser. Put shapes on layers, each with its own mode (line/cut, fill/engrave, or both), power %, speed, passes, fill line interval and angle. Preview the real laser paths with a time estimate, then make G-code that appears in the job list
- **Machine log**: GRBL responses and errors, plus a box for one-off commands (`$$`, `$I`, …)

G-code is streamed with GRBL's character-counting protocol, so the controller's 128-byte buffer stays full and engraving stays smooth.

## Install on the Pi

```bash
git clone https://github.com/<you>/laser-server.git
cd laser-server
./deploy/install.sh
```

The script installs the system packages (OpenCV and pyserial from apt), creates `.venv`, adds your user to the `dialout` and `video` groups, writes `/etc/laser-server.env`, and installs and starts a `laser-server` systemd service.

Then open **http://laser-server.local:8000/**.

### Update

```bash
cd laser-server && git pull && sudo systemctl restart laser-server
```

Re-run `./deploy/install.sh` if dependencies changed (they did in 0.2.0, for the design page).

### Logs

```bash
journalctl -u laser-server -f
```

## Designing

Open **Design** in the top bar.

1. Pick a layer on the right (that's where new shapes go), then a tool on the left (**R**ect, **E**llipse, **L**ine, **T**ext) and drag on the bed. **Import SVG** brings in drawings from Inkscape, Illustrator, etc.
2. Drag to move, use the corner handles to resize and the round handle to rotate (hold Shift to snap to 15°). Exact position, size and rotation are in the Object panel. Arrow keys nudge 1 mm (Shift: 10 mm).
3. Open a layer's ▾ settings for mode, power, speed, passes and, for fill, the line interval and angle. Untick a layer to leave it out.
4. **Preview** shows the actual laser paths, estimated time and size. **Make G-code** saves the design and creates `<name>.gcode` in the job list. Then go to **Control**, frame it and run it.

Positions are in mm from the work origin (bottom-left, Y up), the same as the machine. Use **Set origin here** on the Control page to put the origin where your material's corner is.

Power % is scaled to GRBL's `$30` (max spindle) and the bed size comes from `$130`/`$131`; both are read from the controller. Designs are generated for GRBL laser mode (`$32=1`, dynamic power `M4`). If laser mode is off, the generator switches the laser off for every travel move instead.

Keyboard: V/R/E/L/T tools, Del delete, Ctrl+D duplicate, Ctrl+Z / Ctrl+Shift+Z undo/redo, Ctrl+S save, Space+drag or middle-drag to pan, scroll to zoom.

## Configuration

Edit `/etc/laser-server.env` and run `sudo systemctl restart laser-server`.

| Variable | Default | Meaning |
|---|---|---|
| `LASER_HTTP_PORT` | `8000` | Web server port |
| `LASER_SERIAL_PORT` | `auto` | Serial device; `auto` picks the first USB serial port |
| `LASER_BAUD` | `115200` | GRBL baud rate |
| `LASER_CAMERA` | `0` | Camera index (`/dev/video0`) or device path |
| `LASER_CAMERA_WIDTH` / `_HEIGHT` / `_FPS` | `1280` / `720` / `10` | Capture settings |
| `LASER_JOBS_DIR` | `~/laser-jobs` | Where uploaded files are stored |
| `LASER_DESIGNS_DIR` | `~/laser-designs` | Where saved designs are stored |
| `LASER_BED_WIDTH` / `_HEIGHT` | `400` / `400` | Bed size (mm) if GRBL doesn't report `$130`/`$131` |
| `LASER_S_MAX` | `1000` | Power at 100% if GRBL doesn't report `$30` |
| `LASER_PASSWORD` | *(empty)* | If set, requires HTTP basic auth with this password |
| `LASER_MAX_UPLOAD_MB` | `200` | Upload size limit |

Useful checks on the Pi: `ls /dev/serial/by-id/` for the laser, `v4l2-ctl --list-devices` for the camera.

## Safety

- **STOP** (top right) sends a GRBL soft reset, which stops motion and turns the laser off immediately.
- **Stop job** does a feed hold first, then a reset, so GRBL keeps its position.
- If GRBL reports an `error:` during a job, the job is halted rather than continued.
- **Frame** traces the job's bounding box with `M5` (laser off) using jog moves.
- Jogging and console commands are refused while a job is running.
- There is no authentication by default. Keep it on a trusted network, or set `LASER_PASSWORD`. Never leave a running laser unattended.

## Development

Run it anywhere (no laser needed to open the UI):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,camera]'
python -m laser_server
pytest
```

The tests use a simulated GRBL controller (`tests/fake_grbl.py`) to check streaming, buffer limits, stop, and error handling.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/status` | Connection, state, positions, job progress, camera |
| GET | `/api/console?since=<t>` | Log entries after timestamp |
| POST | `/api/jog` | `{"x": 10, "y": 0, "feed": 3000}` relative jog (mm) |
| POST | `/api/jog/cancel`, `/api/home`, `/api/unlock`, `/api/reset` | |
| POST | `/api/origin/set`, `/api/origin/goto` | Work origin (G54) |
| POST | `/api/command` | `{"command": "$$"}` |
| GET/POST | `/api/jobs` | List / upload (multipart `file`) |
| GET/DELETE | `/api/jobs/{name}` | Download / delete |
| GET | `/api/jobs/{name}/bounds` | Burn-area bounding box |
| POST | `/api/jobs/{name}/frame`, `/api/jobs/{name}/run` | |
| POST | `/api/job/pause`, `/api/job/resume`, `/api/job/stop` | |
| GET | `/camera/stream.mjpg`, `/camera/snapshot.jpg` | |
| GET | `/api/machine`, `/api/fonts` | Machine limits; available fonts |
| GET/PUT/DELETE | `/api/designs[/{name}]` | List / load / save / delete designs |
| POST | `/api/design/preview` | Design JSON → toolpaths, bounds, time estimate |
| POST | `/api/design/gcode` | `{"design": …, "filename": "x"}` → writes `x.gcode` to jobs |
| POST | `/api/text`, `/api/import/svg` | Text outline; SVG → polylines (mm) |

Interactive API docs: `http://laser-server.local:8000/docs`.
