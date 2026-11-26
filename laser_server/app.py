"""FastAPI web app: REST API + static single-page UI."""

from __future__ import annotations

import base64
import json
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
from .design import DesignError, generate_gcode
from .gcode import job_bounds
from .grbl import Grbl, GrblError
from .images import ImageError, ImageStore
from .importers import ImportError_, default_font, list_fonts, svg_to_paths, text_to_paths

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


class TextRequest(BaseModel):
    text: str = Field(..., max_length=2000)
    font: Optional[str] = None
    size: float = Field(10, gt=0, le=1000)
    align: str = "left"


class GcodeRequest(BaseModel):
    design: dict
    filename: Optional[str] = None


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
    settings.designs_dir.mkdir(parents=True, exist_ok=True)
    images = ImageStore(settings.designs_dir / "images")
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

    @app.exception_handler(DesignError)
    @app.exception_handler(ImportError_)
    @app.exception_handler(ImageError)
    async def design_error(_: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def job_path(name: str) -> Path:
        path = settings.jobs_dir / _safe_name(name)
        if not path.is_file():
            raise HTTPException(404, "No such job file")
        return path

    # ---------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/design", include_in_schema=False)
    def design_page():
        return FileResponse(STATIC_DIR / "design.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # --------------------------------------------------------------- status
    @app.get("/api/status")
    def status():
        data = grbl.snapshot()
        data["camera"] = {"available": camera.available, "error": camera.error}
        data["frame_power"] = settings.frame_power
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
    def frame(name: str, feed: float = 3000, laser: bool = False):
        b = job_bounds(job_path(name))
        if b is None:
            raise HTTPException(422, "No moves found in file")
        grbl.frame(b, feed, settings.frame_power if laser else 0.0)
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

    # -------------------------------------------------------------- designs
    def machine_info() -> dict:
        return {
            "s_max": grbl.setting(30, settings.s_max),
            "laser_mode": grbl.setting(32, 1) == 1,
            "laser_mode_known": "32" in grbl.settings,
            "bed_width": grbl.setting(130, 0) or settings.bed_width,
            "bed_height": grbl.setting(131, 0) or settings.bed_height,
            "rapid": grbl.setting(110, 6000) or 6000,
        }

    def design_path(name: str) -> Path:
        stem = _SAFE_NAME_RE.sub("_", Path(name).stem).strip(" .")
        if not stem:
            raise HTTPException(400, "Bad design name")
        return settings.designs_dir / f"{stem}.json"

    def _generate(design: dict, **kw):
        m = machine_info()
        return generate_gcode(design, s_max=m["s_max"], laser_mode=m["laser_mode"],
                              rapid_mm_min=m["rapid"], image_loader=images.load, **kw)

    def _stats(result) -> dict:
        return {"bounds": result.bounds, "cut_mm": result.cut_mm, "travel_mm": result.travel_mm,
                "est_seconds": result.est_seconds, "lines": result.lines}

    @app.get("/api/machine")
    def machine():
        return machine_info()

    @app.get("/api/fonts")
    def fonts():
        return {"fonts": list(list_fonts()), "default": default_font()}

    @app.post("/api/text")
    def text_outline(req: TextRequest):
        paths = text_to_paths(req.text, req.font, req.size, align=req.align)
        return {"paths": [[[round(x, 4), round(y, 4)] for x, y in pl] for pl in paths]}

    @app.post("/api/import/svg")
    async def import_svg(file: UploadFile = File(...)):
        data = await file.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(413, "SVG is larger than 20 MB")
        paths, w, h = svg_to_paths(data)
        return {"name": Path(file.filename or "import").stem, "paths": paths, "w": w, "h": h}

    @app.get("/api/designs")
    def list_designs():
        files = sorted(settings.designs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.stem, "modified": p.stat().st_mtime} for p in files]

    @app.get("/api/designs/{name}")
    def get_design(name: str):
        path = design_path(name)
        if not path.is_file():
            raise HTTPException(404, "No such design")
        return FileResponse(path, media_type="application/json")

    @app.put("/api/designs/{name}")
    def save_design(name: str, design: dict):
        path = design_path(name)
        design["name"] = path.stem
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(design))
        tmp.replace(path)
        return {"name": path.stem}

    @app.delete("/api/designs/{name}")
    def delete_design(name: str):
        path = design_path(name)
        if not path.is_file():
            raise HTTPException(404, "No such design")
        path.unlink()
        return {"ok": True}

    @app.post("/api/design/preview")
    def preview(design: dict):
        result = _generate(design, out=False, preview_images=True)
        for tp in result.toolpaths:  # round to keep the response small
            tp["paths"] = [[(round(x, 2), round(y, 2)) for x, y in pl] for pl in tp["paths"]]
        return {"toolpaths": result.toolpaths, **_stats(result)}

    @app.post("/api/design/gcode")
    def design_gcode(req: GcodeRequest):
        stem = Path(req.filename or req.design.get("name") or "design").stem
        name = _safe_name(f"{stem}.gcode")
        if grbl.job.running and grbl.job.name == name:
            raise HTTPException(409, "A job with that name is running")
        dest = settings.jobs_dir / name
        tmp = dest.with_suffix(".gcode.part")
        try:
            with tmp.open("w", buffering=1024 * 1024) as f:
                result = _generate(req.design, out=f)
            if result.bounds is None:
                raise HTTPException(422, "Nothing to burn: no objects on enabled layers")
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        return {"name": name, **_stats(result)}

    @app.post("/api/images")
    async def upload_image(file: UploadFile = File(...)):
        data = await file.read(40 * 1024 * 1024 + 1)
        if len(data) > 40 * 1024 * 1024:
            raise HTTPException(413, "Image is larger than 40 MB")
        info = images.save(data)
        info["name"] = Path(file.filename or "image").stem
        return info

    @app.get("/api/images/{image_id}")
    def get_image(image_id: str):
        path = images.path(image_id)
        if not path.is_file():
            raise HTTPException(404, "No such image")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=31536000, immutable"})

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
