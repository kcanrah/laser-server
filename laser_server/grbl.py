"""GRBL 1.1 controller over a serial port.

Uses GRBL's character-counting streaming protocol so the controller's
128-byte receive buffer stays full during jobs, polls status with the
`?` realtime command, and exposes thread-safe helpers for jogging,
homing, and running/pausing/stopping G-code jobs.
"""

from __future__ import annotations

import glob
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

RX_BUFFER_SIZE = 128

# Realtime commands (sent immediately, bypass the line buffer)
RT_STATUS = b"?"
RT_FEED_HOLD = b"!"
RT_CYCLE_START = b"~"
RT_SOFT_RESET = b"\x18"
RT_JOG_CANCEL = b"\x85"

_STATUS_RE = re.compile(r"^<(.*)>$")
_COMMENT_PAREN_RE = re.compile(r"\(.*?\)")


def clean_gcode_line(line: str) -> str:
    """Strip comments and whitespace. GRBL ignores spaces, and dropping
    them saves room in the controller's receive buffer."""
    line = _COMMENT_PAREN_RE.sub("", line)
    line = line.split(";", 1)[0]
    return "".join(line.split()).upper()


def find_serial_port() -> Optional[str]:
    """Pick the first USB serial device, which is where GRBL boards show up."""
    candidates = sorted(glob.glob("/dev/serial/by-id/*"))
    candidates += sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
    return candidates[0] if candidates else None


@dataclass
class JobState:
    name: Optional[str] = None
    total_lines: int = 0
    lines_sent: int = 0
    lines_done: int = 0
    running: bool = False
    paused: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[str] = None  # "completed" | "stopped" | "error: ..."

    def as_dict(self) -> dict:
        elapsed = None
        if self.started_at:
            elapsed = (self.finished_at or time.time()) - self.started_at
        pct = (100.0 * self.lines_done / self.total_lines) if self.total_lines else 0.0
        return {
            "name": self.name,
            "total_lines": self.total_lines,
            "lines_sent": self.lines_sent,
            "lines_done": self.lines_done,
            "percent": round(pct, 1),
            "running": self.running,
            "paused": self.paused,
            "elapsed": round(elapsed, 1) if elapsed is not None else None,
            "result": self.result,
        }


@dataclass
class MachineStatus:
    state: str = "Disconnected"
    mpos: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    wpos: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    wco: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    feed: float = 0.0
    spindle: float = 0.0
    overrides: list = field(default_factory=lambda: [100, 100, 100])
    alarm: Optional[str] = None
    last_error: Optional[str] = None


class GrblError(RuntimeError):
    pass


class Grbl:
    def __init__(
        self,
        port: str = "auto",
        baud: int = 115200,
        serial_factory: Optional[Callable[[str, int], object]] = None,
        poll_interval: float = 0.25,
    ):
        self.port_setting = port
        self.baud = baud
        self.poll_interval = poll_interval
        self._serial_factory = serial_factory or self._open_pyserial

        self.port: Optional[str] = None
        self.ser = None
        self.version: Optional[str] = None
        self.status = MachineStatus()
        self.job = JobState()
        self.console: deque = deque(maxlen=300)

        self._lock = threading.RLock()
        self._space = threading.Condition(self._lock)  # buffer space / acks
        self._write_lock = threading.Lock()
        self._pending: deque = deque()  # byte lengths of lines awaiting ok/error
        self._buf_used = 0
        self._job_line_marks: deque = deque()  # True for job lines, in pending order
        self._job_stop = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _open_pyserial(port: str, baud: int):
        import serial

        return serial.Serial(port, baud, timeout=0.2, write_timeout=2)

    @property
    def connected(self) -> bool:
        return self.ser is not None

    def start(self) -> None:
        """Start background threads that (re)connect, read, and poll status."""
        self._stop.clear()
        for target in (self._connection_loop, self._poll_loop):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        self.stop_job()
        self._close()

    def _close(self) -> None:
        with self._lock:
            ser, self.ser = self.ser, None
            self.status.state = "Disconnected"
            self._reset_buffer_tracking()
            if self.job.running:
                self._finish_job("error: disconnected")
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _connection_loop(self) -> None:
        while not self._stop.is_set():
            if self.ser is None:
                port = find_serial_port() if self.port_setting == "auto" else self.port_setting
                if port:
                    try:
                        ser = self._serial_factory(port, self.baud)
                        with self._lock:
                            self.ser = ser
                            self.port = port
                            self.status.state = "Connecting"
                            self._reset_buffer_tracking()
                        self._log("info", f"Opened {port} @ {self.baud}")
                        # Many boards reset on open; a soft reset makes the rest consistent.
                        time.sleep(0.5)
                        self._write(RT_SOFT_RESET)
                    except Exception as e:  # noqa: BLE001
                        log.debug("open %s failed: %s", port, e)
                        self.ser = None
            if self.ser is None:
                self._stop.wait(2.0)
                continue
            self._read_once()

    def _read_once(self) -> None:
        ser = self.ser
        try:
            raw = ser.readline()
        except Exception as e:  # noqa: BLE001
            self._log("error", f"Serial read failed: {e}")
            self._close()
            return
        if not raw:
            return
        line = raw.decode("ascii", errors="replace").strip()
        if line:
            self._handle_line(line)

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            if self.ser is not None:
                try:
                    self._write(RT_STATUS)
                except Exception:
                    pass

    # --------------------------------------------------------------- protocol
    def _reset_buffer_tracking(self) -> None:
        with self._lock:
            self._pending.clear()
            self._job_line_marks.clear()
            self._buf_used = 0
            self._space.notify_all()

    def _write(self, data: bytes) -> None:
        ser = self.ser
        if ser is None:
            raise GrblError("Laser is not connected")
        with self._write_lock:
            try:
                ser.write(data)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"Serial write failed: {e}")
                self._close()
                raise GrblError(str(e)) from e

    def _log(self, kind: str, text: str) -> None:
        self.console.append({"t": time.time(), "kind": kind, "text": text})
        if kind == "error":
            log.warning(text)

    def _handle_line(self, line: str) -> None:
        m = _STATUS_RE.match(line)
        if m:
            self._parse_status(m.group(1))
            return
        if line == "ok" or line.startswith("error:"):
            self._handle_ack(line)
            return
        if line.startswith("Grbl ") or line.startswith("GrblHAL "):
            # Controller (re)booted: its buffer is empty now.
            self.version = line
            self._reset_buffer_tracking()
            with self._lock:
                if self.job.running:
                    self._finish_job("error: controller reset")
                self.status.alarm = None
            self._log("info", line)
            return
        if line.startswith("ALARM:"):
            with self._lock:
                self.status.alarm = line
                self.status.state = "Alarm"
                if self.job.running:
                    self._job_stop.set()
                    self._finish_job(f"error: {line}")
            self._log("error", line)
            return
        self._log("rx", line)

    def _handle_ack(self, line: str) -> None:
        with self._lock:
            if self._pending:
                self._buf_used -= self._pending.popleft()
            is_job_line = self._job_line_marks.popleft() if self._job_line_marks else False
            if is_job_line and self.job.running:
                self.job.lines_done += 1
            if line.startswith("error:"):
                self.status.last_error = line
                if is_job_line and self.job.running:
                    self._job_stop.set()
                    self._finish_job(f"error: {line} at line {self.job.lines_done}")
                    # Don't let the rest of the buffered job run (laser may be on).
                    threading.Thread(target=self._halt, daemon=True).start()
            self._space.notify_all()
        if line.startswith("error:"):
            self._log("error", line)

    def _parse_status(self, body: str) -> None:
        parts = body.split("|")
        with self._lock:
            s = self.status
            s.state = parts[0]
            mpos = wpos = None
            for p in parts[1:]:
                key, _, val = p.partition(":")
                nums = val.split(",")
                try:
                    if key == "MPos":
                        mpos = [float(x) for x in nums]
                    elif key == "WPos":
                        wpos = [float(x) for x in nums]
                    elif key == "WCO":
                        s.wco = [float(x) for x in nums]
                    elif key == "FS":
                        s.feed, s.spindle = float(nums[0]), float(nums[1])
                    elif key == "F":
                        s.feed = float(nums[0])
                    elif key == "Ov":
                        s.overrides = [int(x) for x in nums]
                except ValueError:
                    pass
            if mpos is not None:
                s.mpos = mpos
                s.wpos = [m - w for m, w in zip(mpos, s.wco)]
            elif wpos is not None:
                s.wpos = wpos
                s.mpos = [w + o for w, o in zip(wpos, s.wco)]
            if not s.state.startswith("Alarm"):
                s.alarm = None

    def _send_line(self, line: str, job: bool = False, timeout: Optional[float] = 30.0) -> None:
        """Queue one line, blocking until GRBL's buffer has room for it."""
        data = (line + "\n").encode("ascii")
        if len(data) > RX_BUFFER_SIZE:
            raise GrblError(f"Line too long for GRBL buffer: {line[:40]}...")
        deadline = None if timeout is None else time.time() + timeout
        with self._space:
            while self._buf_used + len(data) > RX_BUFFER_SIZE:
                if self.ser is None:
                    raise GrblError("Laser is not connected")
                if job and self._job_stop.is_set():
                    return
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    raise GrblError("Timed out waiting for controller")
                self._space.wait(0.1 if remaining is None else min(0.1, remaining))
            self._pending.append(len(data))
            self._job_line_marks.append(job)
            self._buf_used += len(data)
        self._write(data)
        if job:
            with self._lock:
                self.job.lines_sent += 1
        else:
            self._log("tx", line)

    # ------------------------------------------------------------- public API
    def _require_idle_for_manual(self) -> None:
        if self.job.running:
            raise GrblError("A job is running")
        if self.ser is None:
            raise GrblError("Laser is not connected")

    def send_command(self, cmd: str) -> None:
        cmd = cmd.strip()
        if not cmd:
            return
        self._require_idle_for_manual()
        # Allow sending realtime chars from the console as well.
        if cmd in ("?", "!", "~"):
            self._write(cmd.encode())
            self._log("tx", cmd)
            return
        self._send_line(cmd)

    def jog(self, x: float = 0, y: float = 0, z: float = 0, feed: float = 1000) -> None:
        self._require_idle_for_manual()
        axes = "".join(f"{a}{v:.3f}" for a, v in (("X", x), ("Y", y), ("Z", z)) if v)
        if not axes:
            return
        self._send_line(f"$J=G91G21{axes}F{feed:.0f}")

    def jog_to(self, x: Optional[float], y: Optional[float], feed: float = 3000) -> None:
        self._require_idle_for_manual()
        axes = "".join(f"{a}{v:.3f}" for a, v in (("X", x), ("Y", y)) if v is not None)
        if axes:
            self._send_line(f"$J=G90G21{axes}F{feed:.0f}")

    def jog_cancel(self) -> None:
        self._write(RT_JOG_CANCEL)

    def home(self) -> None:
        self._require_idle_for_manual()
        self._send_line("$H", timeout=120)

    def unlock(self) -> None:
        self._require_idle_for_manual()
        self._send_line("$X")

    def set_origin(self) -> None:
        """Make the current head position the work-coordinate origin (G54)."""
        self._require_idle_for_manual()
        self._send_line("G10L20P1X0Y0")

    def goto_origin(self, feed: float = 3000) -> None:
        self.jog_to(0.0, 0.0, feed)

    def feed_hold(self) -> None:
        self._write(RT_FEED_HOLD)

    def cycle_start(self) -> None:
        self._write(RT_CYCLE_START)

    def soft_reset(self) -> None:
        """Immediate stop. GRBL turns the laser off and flushes its buffer."""
        self._job_stop.set()
        self._write(RT_SOFT_RESET)
        self._reset_buffer_tracking()
        with self._lock:
            if self.job.running:
                self._finish_job("stopped")

    def frame(self, bounds: tuple, feed: float = 3000) -> None:
        """Trace the job's bounding box with the laser OFF."""
        self._require_idle_for_manual()
        xmin, ymin, xmax, ymax = bounds
        self._send_line("M5")
        for x, y in ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)):
            self._send_line(f"$J=G90G21X{x:.3f}Y{y:.3f}F{feed:.0f}")

    # ------------------------------------------------------------------ jobs
    def start_job(self, path: Path) -> None:
        self._require_idle_for_manual()
        if self.status.state.startswith("Alarm"):
            raise GrblError("Controller is in alarm state; home or unlock first")
        lines = [ln for ln in (clean_gcode_line(r) for r in path.read_text(errors="replace").splitlines()) if ln]
        if not lines:
            raise GrblError("File contains no G-code")
        with self._lock:
            self.job = JobState(name=path.name, total_lines=len(lines), running=True, started_at=time.time())
            self._job_stop.clear()
        self._log("info", f"Starting job {path.name} ({len(lines)} lines)")
        t = threading.Thread(target=self._stream_job, args=(lines,), daemon=True, name="job")
        t.start()

    def _stream_job(self, lines: Iterable[str]) -> None:
        try:
            for line in lines:
                if self._job_stop.is_set():
                    return
                self._send_line(line, job=True, timeout=None)
            # Wait for the controller to acknowledge everything we sent.
            with self._space:
                while self.job.running and self.job.lines_done < self.job.lines_sent:
                    if self._job_stop.is_set() or self.ser is None:
                        return
                    self._space.wait(0.2)
            # Then wait for motion to finish.
            while self.job.running and not self._job_stop.is_set():
                if self.status.state == "Idle":
                    break
                time.sleep(self.poll_interval)
            with self._lock:
                if self.job.running:
                    self._finish_job("completed")
        except GrblError as e:
            with self._lock:
                if self.job.running:
                    self._finish_job(f"error: {e}")

    def _finish_job(self, result: str) -> None:
        self.job.running = False
        self.job.paused = False
        self.job.finished_at = time.time()
        self.job.result = result
        self._log("info" if result == "completed" else "error", f"Job {self.job.name}: {result}")

    def pause_job(self) -> None:
        if not self.job.running:
            raise GrblError("No job running")
        self.feed_hold()
        self.job.paused = True

    def resume_job(self) -> None:
        if not self.job.running:
            raise GrblError("No job running")
        self.cycle_start()
        self.job.paused = False

    def stop_job(self) -> None:
        """Stop a running job and turn the laser off."""
        if not self.job.running or self.ser is None:
            return
        self._job_stop.set()
        self._halt()
        with self._lock:
            if self.job.running:
                self._finish_job("stopped")

    def _halt(self) -> None:
        """Feed hold first so GRBL keeps its position, then soft reset, which
        flushes the buffer and turns the laser off."""
        try:
            self.feed_hold()
            deadline = time.time() + 3
            while time.time() < deadline and self.status.state not in ("Hold:0", "Idle", "Alarm"):
                time.sleep(0.05)
            self.soft_reset()
        except GrblError:
            pass

    def snapshot(self) -> dict:
        with self._lock:
            s = self.status
            return {
                "connected": self.connected,
                "port": self.port,
                "version": self.version,
                "state": s.state,
                "alarm": s.alarm,
                "last_error": s.last_error,
                "mpos": s.mpos,
                "wpos": s.wpos,
                "feed": s.feed,
                "spindle": s.spindle,
                "overrides": s.overrides,
                "job": self.job.as_dict(),
            }
