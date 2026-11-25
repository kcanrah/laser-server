"""A simulated GRBL 1.1 controller that behaves like a pyserial port."""

from __future__ import annotations

import queue
import threading
import time


class FakeGrbl:
    RX_BUFFER = 128

    def __init__(self, line_time: float = 0.001):
        self.line_time = line_time
        self.out: queue.Queue = queue.Queue()
        self.rx = bytearray()          # bytes received but not yet "planned"
        self.received_lines: list[str] = []
        self.max_rx_used = 0
        self.overflowed = False
        self.state = "Idle"
        self.pos = [0.0, 0.0, 0.0]
        self.closed = False
        self.error_on = set()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._process, daemon=True)
        self._worker.start()

    # pyserial API
    def write(self, data: bytes) -> int:
        with self._lock:
            for b in data:
                c = bytes([b])
                if c == b"?":
                    self.out.put(f"<{self.state}|MPos:{self.pos[0]:.3f},{self.pos[1]:.3f},{self.pos[2]:.3f}|FS:0,0|WCO:0.000,0.000,0.000>\n")
                elif c == b"!":
                    if self.state in ("Run", "Jog"):
                        self.state = "Hold:0"
                elif c == b"~":
                    if self.state.startswith("Hold"):
                        self.state = "Run"
                elif c == b"\x18":
                    self.rx.clear()
                    self.state = "Idle"
                    self.out.put("\r\nGrbl 1.1h ['$' for help]\n")
                elif c == b"\x85":
                    pass
                else:
                    self.rx += c
                    if len(self.rx) > self.RX_BUFFER:
                        self.overflowed = True
                    self.max_rx_used = max(self.max_rx_used, len(self.rx))
        return len(data)

    def readline(self) -> bytes:
        try:
            return self.out.get(timeout=0.05).encode()
        except queue.Empty:
            return b""

    def close(self) -> None:
        self.closed = True

    def _process(self) -> None:
        while not self.closed:
            line = None
            with self._lock:
                if self.state.startswith("Hold"):
                    pass
                elif b"\n" in self.rx:
                    idx = self.rx.index(b"\n")
                    line = self.rx[:idx].decode().strip()
                    del self.rx[: idx + 1]
                    if self.state == "Idle":
                        self.state = "Run"
            if line is None:
                with self._lock:
                    if self.state == "Run" and not self.rx:
                        self.state = "Idle"
                time.sleep(self.line_time)
                continue
            time.sleep(self.line_time)
            self.received_lines.append(line)
            if line in self.error_on:
                self.out.put("error:20\n")
            else:
                for word in line.replace("$J=", "").split("X")[1:2]:
                    try:
                        self.pos[0] = float(word.split("Y")[0].split("F")[0])
                    except ValueError:
                        pass
                self.out.put("ok\n")
