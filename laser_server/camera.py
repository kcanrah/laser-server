"""USB webcam capture with a shared latest-frame buffer for MJPEG streaming."""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterator, Optional

log = logging.getLogger(__name__)


class Camera:
    def __init__(self, device: str | int = 0, width: int = 1280, height: int = 720,
                 fps: int = 10, jpeg_quality: int = 80):
        self.device = int(device) if str(device).isdigit() else device
        self.width = width
        self.height = height
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.error: Optional[str] = None
        self._frame: Optional[bytes] = None
        self._frame_id = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def available(self) -> bool:
        return self._frame is not None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="camera")
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            import cv2
        except ImportError:
            self.error = "OpenCV (cv2) is not installed"
            log.warning(self.error)
            return

        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2) if isinstance(self.device, int) \
                else cv2.VideoCapture(self.device)
            if not cap.isOpened():
                self.error = f"Cannot open camera {self.device}"
                self._stop.wait(5)
                continue
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.error = None
            log.info("Camera %s opened", self.device)
            interval = 1.0 / max(self.fps, 1)
            failures = 0
            while not self._stop.is_set():
                t0 = time.time()
                ok, frame = cap.read()
                if not ok:
                    failures += 1
                    if failures > 20:
                        self.error = "Camera stopped returning frames"
                        break
                    time.sleep(0.1)
                    continue
                failures = 0
                ok, jpg = cv2.imencode(".jpg", frame, encode_params)
                if ok:
                    with self._cond:
                        self._frame = jpg.tobytes()
                        self._frame_id += 1
                        self._cond.notify_all()
                time.sleep(max(0.0, interval - (time.time() - t0)))
            cap.release()
            self._stop.wait(2)

    def snapshot(self) -> Optional[bytes]:
        return self._frame

    async def mjpeg_async(self, max_seconds: float = 600):
        """Async MJPEG generator: no thread is tied up per viewer, and Starlette
        cancels it when the client disconnects. Streams end after `max_seconds`
        so connections from sleeping laptops don't linger; the page reconnects."""
        import asyncio

        last = -1
        started = time.monotonic()
        poll = min(0.05, 0.5 / max(self.fps, 1))
        while not self._stop.is_set() and time.monotonic() - started < max_seconds:
            frame, fid = self._frame, self._frame_id
            if frame is None or fid == last:
                await asyncio.sleep(poll)
                continue
            last = fid
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")

    def mjpeg(self) -> Iterator[bytes]:
        """Yield multipart MJPEG chunks for a StreamingResponse."""
        last = -1
        while not self._stop.is_set():
            with self._cond:
                self._cond.wait_for(lambda: self._frame_id != last or self._stop.is_set(), timeout=5)
                frame, last = self._frame, self._frame_id
            if frame is None:
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
