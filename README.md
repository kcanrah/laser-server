# laser-server

A small Python web server for running a GRBL laser engraver from a Raspberry Pi, with a live view from a USB webcam.

- **Live camera**: MJPEG stream of the bed, snapshots, and fullscreen (button, **F**, or double-click) with a progress overlay and a STOP button
- **Jobs**: upload G-code (from LightBurn, LaserGRBL, Inkscape, etc.), frame it (laser off, or on at 1% so you can see the outline on the material), then run, pause, resume, or stop it, with progress, time remaining and an estimated finish time
- **Machine control**: home, unlock, jog (buttons or arrow keys), set and go to the work origin, live position and state
- **Design**: draw rectangles, ellipses, lines and text, import SVGs, or import photos and images to engrave, right in the browser, with rulers and snapping. Put shapes on layers, each with its own mode (line/cut, fill/engrave, or both), power %, speed, passes, fill line interval and angle. Preview the real laser paths with a time estimate, then make G-code that appears in the job list
- **Material presets**: save a layer's power, speed, passes, mode and line interval as a named preset per material (e.g. "3mm plywood – Cut"), apply it to any layer in one click, export/import them
- **Test pattern**: a labelled grid of squares at different powers and speeds, with adjustable ranges, grid size, square size and position, to find the right settings for a material
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

Re-run `./deploy/install.sh` if dependencies changed (they did in 0.2.0 and 0.3.0).

### Logs

```bash
journalctl -u laser-server -f
```

## Designing

Open **Design** in the top bar.

1. Pick a layer on the right (that's where new shapes go), then a tool on the left (**R**ect, **E**llipse, **L**ine, **T**ext) and drag on the bed. **Import SVG** brings in drawings from Inkscape, Illustrator, etc. **Image** (or **I**) imports a photo or picture (PNG, JPG, …) to engrave.
2. Drag to move, use the corner handles to resize and the round handle to rotate (hold Shift to snap to 15°). Exact position, size and rotation are in the Object panel. Arrow keys nudge 1 mm (Shift: 10 mm).
3. Open a layer's ▾ settings for mode, power, speed (mm/s), passes and, for fill, the line interval and angle. Untick a layer to leave it out.
   **Negative** (fill layers) engraves the background instead of the shapes: a rectangle around everything on the layer, **Border margin** mm larger, is filled with the shapes left unburned, so text and shapes stand out raised (stamps, slate, dark or coated materials). Holes inside letters are burned as expected. With *Fill + Line* the shapes and the border are also traced. (Images have their own **Invert** in the Object panel.)
   Images go on an **Image** layer (created automatically), which has:
   - **Dither**: *Dithered* (Floyd–Steinberg dots; best for photos), *Threshold* (pure black/white; logos), or *Grayscale* (varies power between **Min** and **Max power**; works on wood with some lasers)
   - **Line interval** (0.1 mm = 254 DPI), **Overscan** (runs past each row's ends with the laser off so speed is even where it burns), **Both directions**

   Each image also has **Invert**, **Brightness** and **Contrast** in the Object panel.
4. **Snap** (top right of the canvas): *Grid* snaps to the chosen spacing; *Objects* lines shapes up with other shapes' edges and centres and with the bed's edges and centre (cyan guides show it). Hold **Alt** while dragging to ignore snapping. The rulers show mm, the cursor position and the selected object's extent.
5. **Preview** shows the actual laser paths, estimated time and size. **Make G-code** saves the design and creates `<name>.gcode` in the job list. Then go to **Control**, frame it and run it.

### Material presets

Open a layer's ▾ settings:

- **★ Apply a preset…** fills in the layer's mode, power, speed, passes and line interval (plus dither/overscan for image presets). The layer shows **★ Material – Name** while it still matches, or **☆ Changed from …** if you've tweaked it, with an **Update preset** button.
- **Save as preset…** saves the layer's settings with a material, a name and optional notes. You can adjust the values in that dialog before saving.

**★ Presets** (top of the Layers panel) lists them by material, where you can apply one to the current layer, edit, delete, and **Export…**/**Import…** them as a JSON file (importing updates presets with the same material and name). In the **Test pattern** panel, **Save its settings as a preset…** lets you record the square you liked.

Presets are stored on the Pi in `~/laser-designs/presets.json`, so every browser sees the same list.

### Test pattern

**Test pattern…** on the Design page opens a panel to burn a grid of squares, with power changing across the columns and speed changing down the rows. Each square is labelled with its values. Set:

- Power % and speed (mm/s) ranges and the number of columns and rows (up to 20 × 20)
- Square size and gap (the overall size and estimated time update as you type), passes, fill line interval
- Mode: fill (engraving), line (outlines, for cut tests; add passes), or both
- Position on the bed, and whether to engrave labels (with their own power and speed)

The pattern is previewed on the bed. **Make G-code** creates `test-pattern.gcode` in the job list; **Open as design** loads it into the editor (one layer per square) if you want to change anything.

Positions are in mm from the work origin (bottom-left, Y up), the same as the machine. Use **Set origin here** on the Control page to put the origin where your material's corner is.

Power % is scaled to GRBL's `$30` (max spindle) and the bed size comes from `$130`/`$131`; both are read from the controller. Designs are generated for GRBL laser mode (`$32=1`, dynamic power `M4`). If laser mode is off, the generator switches the laser off for every travel move instead.

Speeds are shown in mm/s everywhere in the UI (G-code and the API use mm/min).

Keyboard: V/R/E/L/T tools, I import image, Del delete, Ctrl+D duplicate, Ctrl+Z / Ctrl+Shift+Z undo/redo, Ctrl+S save, Space+drag or middle-drag to pan, scroll to zoom.

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
| `LASER_FRAME_POWER` | `1` | Power % for "Laser on while framing" (max 5) |

Time remaining is estimated from the file (distance ÷ feed for every move) when the job starts, then corrected by how fast the job is actually going once it's been running a little while.

Useful checks on the Pi: `ls /dev/serial/by-id/` for the laser, `v4l2-ctl --list-devices` for the camera.

## Safety

- **STOP** (top right) sends a GRBL soft reset, which stops motion and turns the laser off immediately.
- **Stop job** does a feed hold first, then a reset, so GRBL keeps its position.
- If GRBL reports an `error:` during a job, the job is halted rather than continued.
- **Frame** traces the job's bounding box with `M5` (laser off) using jog moves. With **Laser on while framing** ticked it traces with `M3` at `LASER_FRAME_POWER` % (default 1%, capped at 5%) and turns the laser off at the end.
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
| POST/GET | `/api/images`, `/api/images/{id}` | Upload an image for engraving / fetch it |
| GET/POST/PUT/DELETE | `/api/presets[/{id}]` | Material presets `{material, name, notes, settings: {mode, power, speed (mm/min), passes, hatch, …}}` |
| GET/POST | `/api/presets/export`, `/api/presets/import` | Download / upload presets as JSON |
| POST | `/api/test-pattern/{preview,gcode,design}` | Test pattern from `{power_min, power_max, power_steps, speed_min, speed_max, speed_steps, cell, gap, …}` (speeds in mm/s) |

Interactive API docs: `http://laser-server.local:8000/docs`.
