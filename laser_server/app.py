"""FastAPI web app: REST API + static single-page UI."""

from __future__ import annotations

import base64
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .camera import Camera
from .config import Settings
from .gcode import job_bounds
from .grbl import Grbl, GrblError

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_EXT = {".gcode", ".gc", ".nc", ".ngc", ".g", ".txt"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]")


class JogRequest(BaseModel):
    x: float = 0
    y: float = 0
    z: float = 0
    feed: float = Field(1000, gt=0, le=100000)


class CommandRequest(BaseModel):
    command: str = Field(..., max_length=120)


def _safe_name(name: str) -> str:
    name = Path(name).name
    name = _SAFE_NAME_RE.sub("_", name).strip(" .")
    if not name or Path(name).suffix.lower() not in ALLOWED_EXT:
        raise HTTPException(400, f"File must be one of: {', '.join(sorted(ALLOWED_EXT))}")
    return name


def create_app(settings: Optional[Settings] = None, grbl: Optional[Grbl] = None,
               camera: Optional[Camera] = None) -> FastAPI:
    settings = settings or Settings()
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    grbl = grbl or Grbl(settings.serial_port, settings.baud)
    if camera is None:
        camera = Camera(settings.camera_device, settings.camera_width,
                        settings.camera_height, settings.camera_fps)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        grbl.start()
        camera.start()
        yield
        grbl.shutdown()
        camera.shutdown()

    app = FastAPI(title="Laser Server", lifespan=lifespan)
    app.state.grbl = grbl
    app.state.camera = camera
    app.state.settings = settings

    if settings.password:
        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(pw, settings.password)
                except Exception:
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="laser"'})
            return await call_next(request)

    @app.exception_handler(GrblError)
    async def grbl_error(_: Request, exc: GrblError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def job_path(name: str) -> Path:
        path = settings.jobs_dir / _safe_name(name)
        if not path.is_file():
            raise HTTPException(404, "No such job file")
        return path

    # ---------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # --------------------------------------------------------------- status
    @app.get("/api/status")
    def status():
        data = grbl.snapshot()
        data["camera"] = {"available": camera.available, "error": camera.error}
        return data

    @app.get("/api/console")
    def console(since: float = 0):
        return [e for e in list(grbl.console) if e["t"] > since]

    # -------------------------------------------------------------- control
    @app.post("/api/jog")
    def jog(req: JogRequest):
        grbl.jog(req.x, req.y, req.z, req.feed)
        return {"ok": True}

    @app.post("/api/jog/cancel")
    def jog_cancel():
        grbl.jog_cancel()
        return {"ok": True}

    @app.post("/api/home")
    def home():
        grbl.home()
        return {"ok": True}

    @app.post("/api/unlock")
    def unlock():
        grbl.unlock()
        return {"ok": True}

    @app.post("/api/reset")
    def reset():
        grbl.soft_reset()
        return {"ok": True}

    @app.post("/api/origin/set")
    def set_origin():
        grbl.set_origin()
        return {"ok": True}

    @app.post("/api/origin/goto")
    def goto_origin():
        grbl.goto_origin()
        return {"ok": True}

    @app.post("/api/command")
    def command(req: CommandRequest):
        grbl.send_command(req.command)
        return {"ok": True}

    # ----------------------------------------------------------------- jobs
    @app.get("/api/jobs")
    def list_jobs():
        files = [p for p in settings.jobs_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in ALLOWED_EXT]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime}
                for p in files]

    @app.post("/api/jobs")
    async def upload_job(file: UploadFile = File(...)):
        name = _safe_name(file.filename or "")
        dest = settings.jobs_dir / name
        tmp = dest.with_suffix(dest.suffix + ".part")
        limit = settings.max_upload_mb * 1024 * 1024
        size = 0
        with tmp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB")
                out.write(chunk)
        tmp.replace(dest)
        return {"name": name, "size": size}

    @app.get("/api/jobs/{name}")
    def download_job(name: str):
        return FileResponse(job_path(name), filename=name)

    @app.delete("/api/jobs/{name}")
    def delete_job(name: str):
        path = job_path(name)
        if grbl.job.running and grbl.job.name == path.name:
            raise HTTPException(409, "That job is running")
        path.unlink()
        return {"ok": True}

    @app.get("/api/jobs/{name}/bounds")
    def bounds(name: str):
        b = job_bounds(job_path(name))
        if b is None:
            raise HTTPException(422, "No moves found in file")
        return {"xmin": b[0], "ymin": b[1], "xmax": b[2], "ymax": b[3]}

    @app.post("/api/jobs/{name}/frame")
    def frame(name: str, feed: float = 3000):
        b = job_bounds(job_path(name))
        if b is None:
            raise HTTPException(422, "No moves found in file")
        grbl.frame(b, feed)
        return {"xmin": b[0], "ymin": b[1], "xmax": b[2], "ymax": b[3]}

    @app.post("/api/jobs/{name}/run")
    def run_job(name: str):
        grbl.start_job(job_path(name))
        return {"ok": True}

    @app.post("/api/job/pause")
    def pause():
        grbl.pause_job()
        return {"ok": True}

    @app.post("/api/job/resume")
    def resume():
        grbl.resume_job()
        return {"ok": True}

    @app.post("/api/job/stop")
    def stop():
        grbl.stop_job()
        return {"ok": True}

    # --------------------------------------------------------------- camera
    @app.get("/camera/stream.mjpg")
    def stream():
        return StreamingResponse(camera.mjpeg(),
                                 media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/camera/snapshot.jpg")
    def snapshot():
        frame = camera.snapshot()
        if frame is None:
            raise HTTPException(503, camera.error or "No camera frame yet")
        return Response(frame, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition": f'inline; filename="laser-{int(time.time())}.jpg"'})

    return app
