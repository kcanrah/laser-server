# laser-server

A small Python web server for running a GRBL laser engraver from a Raspberry Pi, with a live view from a USB webcam.

- **Live camera**: MJPEG stream of the bed, plus snapshots
- **Jobs**: upload G-code (from LightBurn, LaserGRBL, Inkscape, etc.), frame it with the laser off, then run, pause, resume, or stop it, with a progress readout
- **Machine control**: home, unlock, jog (buttons or arrow keys), set and go to the work origin, live position and state
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

Re-run `./deploy/install.sh` if dependencies changed.

### Logs

```bash
journalctl -u laser-server -f
```

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

Interactive API docs: `http://laser-server.local:8000/docs`.
