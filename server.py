"""Clipdeck local server (Windows and Linux).

Serves the editor at http://127.0.0.1:8765 and does what a browser can't:
saving projects and clips to disk, downloading clips from links with yt-dlp,
converting exports to MP4 with ffmpeg, and (optionally) asking Claude.

Everything stays on this computer. The server only answers requests made to
127.0.0.1/localhost, and every API call must carry the X-Clipdeck header, so
other websites open in your browser can't use it.
"""
from __future__ import annotations

import asyncio
import hashlib
import py_compile
import io
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path

IS_WIN = os.name == "nt"
APP_DIR = Path(__file__).resolve().parent
STATIC = APP_DIR / "static"
if IS_WIN:
    _local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    _roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    DEFAULT_DATA, DEFAULT_CONFIG = _local / "Clipdeck" / "data", _roaming / "Clipdeck" / "config.json"
else:
    DEFAULT_DATA = Path.home() / ".local/share/clipdeck"
    DEFAULT_CONFIG = Path.home() / ".config/clipdeck/config.json"
DATA = Path(os.environ.get("CLIPDECK_DATA") or DEFAULT_DATA)
CONFIG_FILE = Path(os.environ.get("CLIPDECK_CONFIG") or DEFAULT_CONFIG)
PROJECTS, MEDIA = DATA / "projects", DATA / "media"
EXPORTS_DEFAULT = Path.home() / "Videos" / "Clipdeck"
PORT = int(os.environ.get("CLIPDECK_PORT", "8765"))
MAX_CLIP = 20  # seconds per clip, matches the editor
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "127.0.0.1", "localhost"}
MODELS = {"quick": "claude-haiku-5-5", "default": "claude-sonnet-5-5"}
BROWSERS = ("firefox", "chrome", "chromium", "brave", "edge")
for d in (PROJECTS, MEDIA):
    d.mkdir(parents=True, exist_ok=True)

# Started without a console (pythonw on Windows): send output to a log file.
if sys.stdout is None or sys.stderr is None:
    _log = open(DATA / "clipdeck.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

# Keep ffmpeg / yt-dlp from flashing console windows on Windows.
NOWIN = {"creationflags": 0x08000000} if IS_WIN else {}
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def find_ffmpeg() -> str | None:
    """System ffmpeg if installed, otherwise the copy that ships with the imageio-ffmpeg package."""
    if os.environ.get("CLIPDECK_BUNDLED_FFMPEG") != "1":
        found = shutil.which("ffmpeg")
        if found:
            return found
    try:
        import imageio_ffmpeg

        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
        dest = DATA / "bin" / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
        if not dest.exists() or dest.stat().st_size != src.stat().st_size:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            if not IS_WIN:
                dest.chmod(0o755)
        return str(dest)
    except Exception:
        return None


FFMPEG = find_ffmpeg()
FFPROBE = None if os.environ.get("CLIPDECK_BUNDLED_FFMPEG") == "1" else shutil.which("ffprobe")

app = FastAPI(title="Clipdeck", docs_url=None, redoc_url=None, openapi_url=None)
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


# ---------------------------------------------------------------- helpers
def check_id(v: str) -> str:
    if not ID_RE.match(v or ""):
        raise HTTPException(400, "Bad id")
    return v


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # holds the API key
    except OSError:
        pass
    os.replace(tmp, CONFIG_FILE)


def api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY") or load_config().get("anthropic_api_key", "")


def export_dir() -> Path:
    p = Path(load_config().get("export_dir") or EXPORTS_DEFAULT).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def run_quiet(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=CHILD_ENV, **NOWIN)


def probe_duration(path: Path) -> float:
    try:
        if FFPROBE:
            out = run_quiet([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], 30)
            return round(float(out.stdout.strip()), 3)
        if FFMPEG:
            out = run_quiet([FFMPEG, "-hide_banner", "-i", str(path)], 30).stderr
            m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
            if m:
                return round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 3)
    except Exception:
        pass
    return 0.0


def kind_of(name: str, content_type: str | None) -> str | None:
    ct = content_type if content_type and content_type != "application/octet-stream" else (mimetypes.guess_type(name)[0] or "")
    for k in ("video", "image", "audio"):
        if ct.startswith(k):
            return k
    return None


def media_meta(mid: str) -> dict | None:
    try:
        return json.loads((MEDIA / f"{mid}.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def media_file(meta: dict) -> Path:
    return MEDIA / meta["file"]


def register_media(src: Path, project_id: str, name: str, kind: str, extra: dict | None = None) -> dict:
    mid = uuid.uuid4().hex[:12]
    ext = src.suffix.lower() or mimetypes.guess_extension(mimetypes.guess_type(name)[0] or "") or ""
    dest = MEDIA / f"{mid}{ext}"
    shutil.move(str(src), dest)
    meta = {
        "id": mid, "projectId": project_id, "name": name, "type": kind, "file": dest.name,
        "duration": probe_duration(dest) if kind != "image" else 0, "size": dest.stat().st_size,
        "added": int(time.time() * 1000), **(extra or {}),
    }
    write_json(MEDIA / f"{mid}.json", meta)
    return meta


def tool_version(cmd: list[str]) -> str | None:
    try:
        r = run_quiet(cmd, 20)
        return (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else None
    except Exception:
        return None


def _has_curl_cffi() -> bool:
    try:
        import curl_cffi  # noqa: F401  (lets yt-dlp look like a real browser; TikTok needs it)
        return True
    except Exception:
        return False


def open_in_file_manager(path: Path) -> None:
    if IS_WIN:
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- safety
@app.middleware("http")
async def local_only(request: Request, call_next):
    # Refuse requests that arrive under another host name (blocks DNS-rebinding tricks).
    if request.headers.get("host", "") not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Clipdeck only answers on 127.0.0.1"}, status_code=403)
    # API calls need our header. Browsers won't let other sites add it without a CORS
    # preflight, which this server never approves.
    if request.url.path.startswith("/api/") and request.headers.get("x-clipdeck") != "1":
        return JSONResponse({"detail": "Missing X-Clipdeck header"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------- status & settings
@app.get("/api/status")
def status():
    cfg = load_config()
    ff = tool_version([FFMPEG, "-version"]) if FFMPEG else None
    return {
        "ai": bool(api_key()),
        "aiFromEnv": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "ytdlp": tool_version([sys.executable, "-m", "yt_dlp", "--version"]),
        "impersonate": _has_curl_cffi(),
        "ffmpeg": ff,
        "exportDir": str(export_dir()),
        "dataDir": str(DATA),
        "configPath": str(CONFIG_FILE),
        "browser": cfg.get("browser", ""),
        "fonts": cfg.get("fonts", []),
        "platform": "windows" if IS_WIN else "linux",
        "maxClip": MAX_CLIP,
    }


@app.post("/api/settings")
async def settings(request: Request):
    body = await request.json()
    cfg = load_config()
    if "apiKey" in body:
        key = str(body["apiKey"]).strip()
        if key:
            cfg["anthropic_api_key"] = key
        else:
            cfg.pop("anthropic_api_key", None)
    if "exportDir" in body:
        cfg["export_dir"] = str(body["exportDir"]).strip()
    if "fonts" in body:
        fonts = []
        for f in body["fonts"] or []:
            name = str(f.get("name", "")).strip()
            weights = sorted({int(w) for w in f.get("weights", []) if str(w).isdigit() and int(w) in range(100, 1000, 100)})
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9 ]{0,49}$", name) and weights:
                fonts.append({"name": name, "weights": weights})
        cfg["fonts"] = fonts[:60]
    if "fontLibrary" in body:
        cfg["font_library"] = str(body["fontLibrary"]).strip()
        LIB_CACHE.update(root=None, items=[])
    if "browser" in body:
        if body["browser"] not in ("",) + BROWSERS:
            raise HTTPException(400, "Unknown browser")
        cfg["browser"] = body["browser"]
    save_config(cfg)
    return status()


@app.post("/api/update-ytdlp")
async def update_ytdlp():
    r = await asyncio.to_thread(run_quiet, [sys.executable, "-m", "pip", "install", "-U", "--quiet", "yt-dlp[default,curl-cffi]"], 300)
    if r.returncode != 0:
        raise HTTPException(500, (r.stdout + r.stderr)[-600:] or "Update failed")
    return status()


@app.post("/api/shutdown")
def shutdown():
    for job in JOBS.values():
        proc = job.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
    threading.Timer(0.4, lambda: os._exit(0)).start()
    return {"ok": True}


# ---------------------------------------------------------------- projects
@app.get("/api/projects")
def list_projects():
    out = []
    for f in PROJECTS.glob("*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


@app.put("/api/projects/{pid}")
async def save_project(pid: str, request: Request):
    check_id(pid)
    body = await request.json()
    if body.get("id") != pid:
        raise HTTPException(400, "Id mismatch")
    write_json(PROJECTS / f"{pid}.json", body)
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    check_id(pid)
    (PROJECTS / f"{pid}.json").unlink(missing_ok=True)
    removed = 0
    for f in MEDIA.glob("*.json"):
        meta = media_meta(f.stem)
        if meta and meta.get("projectId") == pid:
            media_file(meta).unlink(missing_ok=True)
            f.unlink(missing_ok=True)
            removed += 1
    return {"ok": True, "mediaRemoved": removed}


@app.post("/api/projects/{pid}/copy-media")
async def copy_media(pid: str, request: Request):
    """Duplicate a project's clips under a new project id. Returns {oldId: newMeta}."""
    check_id(pid)
    body = await request.json()
    src_pid = check_id(body.get("from", ""))
    mapping = {}
    for f in MEDIA.glob("*.json"):
        meta = media_meta(f.stem)
        if not meta or meta.get("projectId") != src_pid:
            continue
        tmp = Path(tempfile.mkdtemp()) / ("x" + Path(meta["file"]).suffix)
        shutil.copy2(media_file(meta), tmp)
        extra = {k: v for k, v in meta.items() if k == "source"}
        mapping[meta["id"]] = register_media(tmp, pid, meta["name"], meta["type"], extra)
    return mapping


# ---------------------------------------------------------------- media
@app.get("/api/media")
def list_media(project: str):
    check_id(project)
    out = []
    for f in MEDIA.glob("*.json"):
        meta = media_meta(f.stem)
        if meta and meta.get("projectId") == project and media_file(meta).exists():
            out.append(meta)
    return sorted(out, key=lambda m: m.get("added", 0))


@app.post("/api/media")
async def upload_media(projectId: str = Form(...), file: UploadFile = File(...)):
    check_id(projectId)
    name = Path(file.filename or "clip").name
    kind = kind_of(name, file.content_type)
    if not kind:
        raise HTTPException(415, "Only video, image and audio files can be added.")
    tmpdir = Path(tempfile.mkdtemp(prefix="clipdeck-"))
    tmp = tmpdir / ("upload" + Path(name).suffix.lower())
    with tmp.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    meta = await asyncio.to_thread(register_media, tmp, projectId, name, kind)
    shutil.rmtree(tmpdir, ignore_errors=True)
    return meta


@app.delete("/api/media/{mid}")
def delete_media(mid: str):
    check_id(mid)
    meta = media_meta(mid)
    if meta:
        media_file(meta).unlink(missing_ok=True)
        (MEDIA / f"{mid}.json").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/media/{mid}")
def get_media(mid: str, request: Request):
    """Serve a clip with HTTP range support so the editor can seek inside videos."""
    check_id(mid)
    meta = media_meta(mid)
    if not meta or not media_file(meta).exists():
        raise HTTPException(404, "Clip not found")
    return serve_with_ranges(media_file(meta), request)


def serve_with_ranges(path: Path, request: Request):
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    m = re.match(r"bytes=(\d*)-(\d*)$", request.headers.get("range") or "")
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return FileResponse(path, media_type=ctype, headers=headers)
    if m.group(1) == "":  # suffix range: last N bytes
        start, end = max(0, size - int(m.group(2))), size - 1
    else:
        start = int(m.group(1))
        end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    length = end - start + 1

    def body():
        with path.open("rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(length)})
    return StreamingResponse(body(), status_code=206, media_type=ctype, headers=headers)


# ---------------------------------------------------------------- downloading from links
JOBS: dict[str, dict] = {}
DL_SLOTS = threading.Semaphore(2)  # two downloads at a time keeps sites from throttling
PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%")


def parse_t(v) -> float | None:
    v = str(v or "").strip()
    if not v:
        return None
    try:
        secs = 0.0
        for part in v.split(":"):
            secs = secs * 60 + float(part)
        return secs
    except ValueError:
        return None


def run_fetch(job_id: str, req: dict) -> None:
    """Runs in a worker thread: download one clip with yt-dlp and register it."""
    job = JOBS[job_id]
    with DL_SLOTS:
        if job.get("cancelled"):
            job.update(state="error", error="Cancelled", finished=time.time())
            return
        job["state"] = "downloading"
        tmpdir = Path(tempfile.mkdtemp(prefix="clipdeck-dl-"))
        a, b = parse_t(req.get("from")), parse_t(req.get("to"))
        a0 = a if a is not None and a >= 0 else 0.0
        b0 = b if b is not None and b > a0 else a0 + MAX_CLIP
        b0 = min(b0, a0 + MAX_CLIP)
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--no-playlist", "--newline", "--progress", "--no-warnings", "--no-colors",
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format", "mp4",
            "--download-sections", f"*{a0:.2f}-{b0:.2f}", "--force-keyframes-at-cuts",
            "--print", "after_move:CDTITLE %(title)s",
            "--print", "after_move:CDFILE %(filepath)s",
            "-o", str(tmpdir / "clip.%(ext)s"),
        ]
        if FFMPEG:
            cmd += ["--ffmpeg-location", str(Path(FFMPEG).parent)]
        browser = req.get("browser") or load_config().get("browser", "")
        if browser in BROWSERS:
            cmd += ["--cookies-from-browser", browser]
        cmd.append(req["url"])
        title, filepath, tail = "", "", []
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=CHILD_ENV, **NOWIN)
            job["proc"] = proc
            assert proc.stdout
            for line in io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace"):
                line = line.rstrip()
                if line.startswith("CDTITLE "):
                    title = line[8:]
                elif line.startswith("CDFILE "):
                    filepath = line[7:]
                elif (mm := PCT_RE.search(line)):
                    job["progress"] = float(mm.group(1))
                elif "frame=" in line or "time=" in line:
                    job["state"] = "cutting"
                if line.strip():
                    tail = (tail + [line])[-12:]
            rc = proc.wait()
            if job.get("cancelled"):
                raise RuntimeError("Cancelled")
            files = [Path(filepath)] if filepath and Path(filepath).exists() else sorted(
                p for p in tmpdir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))
            )
            if rc != 0 or not files:
                raise RuntimeError(friendly_error("\n".join(tail)))
            job["state"] = "saving"
            label = req.get("label") or "clip"
            name = f"{label} · {title}"[:120] if title else f"{label}{files[0].suffix}"
            meta = register_media(files[0], req["projectId"], name, "video",
                                  {"source": {"url": req["url"], "from": a0, "to": b0, "title": title}})
            job.update(state="done", progress=100, media=meta)
        except Exception as e:  # noqa: BLE001 - reported to the editor
            job.update(state="error", error=str(e) or "Download failed")
        finally:
            job.pop("proc", None)
            shutil.rmtree(tmpdir, ignore_errors=True)
            job["finished"] = time.time()


def friendly_error(log: str) -> str:
    low = log.lower()
    if "unexpected response from webpage request" in low or "unable to extract webpage video data" in low \
            or "unable to extract challenge data" in low:
        return ("TikTok blocked the request. Update yt-dlp in Settings and try again. If it keeps failing, "
                "sign in to TikTok in Firefox and pick Firefox under Sign-in.")
    if "impersonat" in low and ("not available" in low or "no impersonate target" in low):
        return "Clipdeck's browser imitation is missing. Run the Clipdeck setup file again to restore it."
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube wants a sign-in for this one. Pick a browser under Sign-in in the Links tab and try again."
    if "could not copy" in low and "cookie" in low or "failed to decrypt" in low or "dpapi" in low:
        return "Couldn't read that browser's sign-in. Use Firefox (Chrome and Edge lock theirs on Windows) and try again."
    if "login" in low or "log in" in low:
        return "This site needs you signed in. Pick the browser you're signed in with under Sign-in and try again."
    if "private" in low:
        return "This video is private."
    if "unsupported url" in low:
        return "yt-dlp doesn't recognise this link."
    if "unable to extract" in low or "http error 403" in low:
        return "The site blocked the download. Update yt-dlp in Settings and try again."
    if "unable to download webpage" in low or ("connection" in low and "failed" in low):
        return "Couldn't reach that site. Check your internet connection and the link."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "ffmpeg is missing. Reinstall Clipdeck to restore it."
    if "ffmpeg" in low and "exited with code" in low:
        return "ffmpeg couldn't cut this video. Try different start and end times, or update yt-dlp in Settings."
    last = [ln for ln in log.splitlines() if "ERROR" in ln]
    msg = last[-1] if last else log.splitlines()[-1] if log else "Download failed"
    return msg.replace("ERROR: ", "").split("; please report")[0][:300]


@app.post("/api/fetch")
async def fetch_clip(request: Request):
    req = await request.json()
    url = str(req.get("url", "")).strip()
    if not re.match(r"^https?://\S+$", url):
        raise HTTPException(400, "That isn't a web link.")
    check_id(req.get("projectId", ""))
    req["url"] = url
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"id": job_id, "state": "queued", "progress": 0, "sceneId": req.get("sceneId"), "started": time.time()}
    threading.Thread(target=run_fetch, args=(job_id, req), daemon=True).start()
    for k in [k for k, j in JOBS.items() if j.get("finished") and time.time() - j["finished"] > 3600]:
        JOBS.pop(k, None)
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return {k: v for k, v in job.items() if k != "proc"}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = JOBS.get(job_id)
    if job and job["state"] not in ("done", "error"):
        job["cancelled"] = True
        proc = job.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
    return {"ok": True}


# ---------------------------------------------------------------- your own font files
FONTS_DIR = DATA / "fonts"
FONTS_INDEX = FONTS_DIR / "fonts.json"
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}


def load_font_index() -> list[dict]:
    try:
        return json.loads(FONTS_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def read_font_meta(path: Path) -> dict | None:
    """Family name, weight (or weight range for variable fonts) and style from a font file."""
    from fontTools.ttLib import TTFont

    try:
        f = TTFont(str(path), lazy=True)
    except Exception:
        return None
    try:
        names = f["name"]
        family = (names.getDebugName(16) or names.getDebugName(1) or "").strip()
        if not family:
            return None
        os2 = f["OS/2"] if "OS/2" in f else None
        italic = bool(os2 and os2.fsSelection & 1) or "italic" in (names.getDebugName(17) or names.getDebugName(2) or "").lower()
        weight: int | list[int] = int(round((os2.usWeightClass if os2 else 400) / 100) * 100) or 400
        if "fvar" in f:
            axis = next((a for a in f["fvar"].axes if a.axisTag == "wght"), None)
            if axis:
                weight = [int(axis.minValue), int(axis.maxValue)]
        return {"family": family, "weight": weight, "style": "italic" if italic else "normal"}
    finally:
        f.close()


def fonts_payload() -> list[dict]:
    """One entry per family: the faces to register in the browser and the weights it offers."""
    fams: dict[str, dict] = {}
    for face in load_font_index():
        if not (FONTS_DIR / face["file"]).exists():
            continue
        fam = fams.setdefault(face["family"], {"family": face["family"], "faces": [], "weights": set()})
        w = face["weight"]
        fam["faces"].append({"file": face["file"], "style": face["style"],
                             "weight": f"{w[0]} {w[1]}" if isinstance(w, list) else str(w)})
        if face["style"] == "normal":
            if isinstance(w, list):
                fam["weights"].update(x for x in range(100, 1000, 100) if w[0] <= x <= w[1])
            else:
                fam["weights"].add(w)
    out = []
    for fam in fams.values():
        fam["weights"] = sorted(fam["weights"]) or [400]
        out.append(fam)
    return sorted(out, key=lambda x: x["family"].lower())


@app.get("/api/fonts")
def list_fonts():
    return fonts_payload()


@app.post("/api/fonts")
async def upload_fonts(files: list[UploadFile] = File(...)):
    import zipfile

    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    index = load_font_index()
    added, skipped = [], []
    tmpdir = Path(tempfile.mkdtemp(prefix="clipdeck-font-"))
    try:
        candidates: list[tuple[str, Path]] = []
        for up in files:
            name = Path(up.filename or "font").name
            dest = tmpdir / uuid.uuid4().hex
            with dest.open("wb") as fh:
                while chunk := await up.read(1 << 20):
                    fh.write(chunk)
            if name.lower().endswith(".zip"):
                # Google Fonts zips hold a variable font plus a static/ folder of single weights.
                try:
                    with zipfile.ZipFile(dest) as z:
                        members = [m for m in z.namelist() if Path(m).suffix.lower() in FONT_EXTS and not m.startswith("__MACOSX")]
                        has_static = any("/static/" in "/" + m for m in members)
                        for m in members:
                            if has_static and "variablefont" in m.lower():
                                continue  # the static files cover the same weights more reliably
                            out = tmpdir / (uuid.uuid4().hex + Path(m).suffix.lower())
                            out.write_bytes(z.read(m))
                            candidates.append((Path(m).name, out))
                except zipfile.BadZipFile:
                    skipped.append(f"{name} (not a valid zip)")
            elif Path(name).suffix.lower() in FONT_EXTS:
                out = dest.with_suffix(Path(name).suffix.lower())
                dest.rename(out)
                candidates.append((name, out))
            else:
                skipped.append(f"{name} (not a font file)")
        await asyncio.to_thread(import_font_paths, candidates, index, added, skipped)
        write_json(FONTS_INDEX, index)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"added": sorted(set(added)), "faces": len(added), "skipped": skipped, "fonts": fonts_payload()}


def import_font_paths(candidates, index, added, skipped, move=True) -> None:
    """Read each font file and file it in Clipdeck's font folder, skipping duplicates."""
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for orig, path in candidates:
        meta = read_font_meta(path)
        if not meta:
            skipped.append(f"{orig} (couldn't read it)")
            continue
        if any(f["family"] == meta["family"] and f["weight"] == meta["weight"] and f["style"] == meta["style"] for f in index):
            skipped.append(f"{orig} (already added)")
            continue
        fid = uuid.uuid4().hex[:12] + path.suffix.lower()
        (shutil.move if move else shutil.copy2)(str(path), FONTS_DIR / fid)
        index.append({**meta, "file": fid, "source": orig})
        added.append(meta["family"])


# ----- a downloaded copy of Google's font collection (github.com/google/fonts), searched in place
LIB_CACHE: dict = {"root": None, "items": []}
NAME_RE = re.compile(r'^name:\s*"([^"]+)"', re.M)
CAT_RE = re.compile(r'^category:\s*"([^"]+)"', re.M)


def library_root() -> Path | None:
    configured = load_config().get("font_library")
    guesses = [Path(configured)] if configured else []
    dl = Path.home() / "Downloads"
    guesses += [dl / "fonts-main" / "fonts-main", dl / "fonts-main", dl / "fonts"]
    for g in guesses:
        if (g / "ofl").is_dir():
            return g
    return None


def scan_library(root: Path) -> list[dict]:
    if LIB_CACHE["root"] == str(root):
        return LIB_CACHE["items"]
    items = []
    for lic in ("ofl", "apache", "ufl"):
        base = root / lic
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if not d.is_dir():
                continue
            family, cat = d.name, ""
            try:
                txt = (d / "METADATA.pb").read_text(encoding="utf-8", errors="replace")
                if (m := NAME_RE.search(txt)):
                    family = m.group(1)
                if (m := CAT_RE.search(txt)):
                    cat = m.group(1).replace("_", " ").title()
            except OSError:
                pass
            items.append({"family": family, "category": cat, "dir": f"{lic}/{d.name}"})
    items.sort(key=lambda x: x["family"].lower())
    LIB_CACHE.update(root=str(root), items=items)
    return items


@app.get("/api/library")
def search_library(q: str = "", limit: int = 40):
    root = library_root()
    if not root:
        return {"found": False, "path": load_config().get("font_library", ""), "count": 0, "results": []}
    items = scan_library(root)
    ql = q.strip().lower()
    hits = [i for i in items if ql in i["family"].lower()] if ql else [i for i in items if i["category"] == "Display"]
    hits.sort(key=lambda i: (not i["family"].lower().startswith(ql), i["family"].lower()))
    have = {f["family"] for f in load_font_index()}
    return {"found": True, "path": str(root), "count": len(items),
            "results": [{**i, "added": i["family"] in have} for i in hits[:max(1, min(limit, 100))]]}


@app.post("/api/library/add")
async def add_from_library(request: Request):
    body = await request.json()
    root = library_root()
    if not root:
        raise HTTPException(404, "Clipdeck can't find your Google Fonts folder. Set it in Settings.")
    rel = str(body.get("dir", ""))
    if not re.match(r"^(ofl|apache|ufl)/[A-Za-z0-9_.-]+$", rel):
        raise HTTPException(400, "Bad font folder")
    d = root / rel
    if not d.is_dir():
        raise HTTPException(404, "That font folder isn't there any more.")
    files = [p for p in d.glob("*") if p.suffix.lower() in FONT_EXTS and "italic" not in p.name.lower()]
    static = [p for p in (d / "static").glob("*") if p.suffix.lower() in FONT_EXTS and "italic" not in p.name.lower()] if (d / "static").is_dir() else []
    chosen = static or files
    if not chosen:
        raise HTTPException(404, "No font files in that folder.")
    index, added, skipped = load_font_index(), [], []
    await asyncio.to_thread(import_font_paths, [(p.name, p) for p in chosen], index, added, skipped, False)
    write_json(FONTS_INDEX, index)
    return {"added": sorted(set(added)), "faces": len(added), "skipped": skipped, "fonts": fonts_payload()}


@app.delete("/api/fonts/{family}")
def delete_font(family: str):
    index = load_font_index()
    keep = []
    for f in index:
        if f["family"] == family:
            (FONTS_DIR / f["file"]).unlink(missing_ok=True)
        else:
            keep.append(f)
    write_json(FONTS_INDEX, keep)
    return {"fonts": fonts_payload()}


@app.get("/fonts/{fid}")
def get_font(fid: str):
    if not re.match(r"^[0-9a-f]{12}\.(ttf|otf|woff2?)$", fid) or not (FONTS_DIR / fid).exists():
        raise HTTPException(404, "Font not found")
    ctype = {"ttf": "font/ttf", "otf": "font/otf", "woff": "font/woff", "woff2": "font/woff2"}[fid.rsplit(".", 1)[1]]
    return FileResponse(FONTS_DIR / fid, media_type=ctype, headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------- export
# The editor draws every frame itself (not in real time) and streams them here as JPEGs;
# ffmpeg encodes them to H.264, then the audio is built straight from the original clip files.
EXPORTS: dict[str, dict] = {}
PROXY_DIR = DATA / "proxies"
PROXY_CODEC = os.environ.get("CLIPDECK_PROXY_CODEC", "h264")
WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_filename(name: str) -> str:
    name = WIN_BAD.sub("", str(name or "")).strip().rstrip(". ")
    name = re.sub(r"\s+", " ", name)[:120].strip()
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.match(r"^(COM|LPT)\d$", name.upper()):
        name = "_" + name
    return name or "ranking"


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    p = folder / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = folder / f"{stem} ({n}){ext}"
        n += 1
    return p


def has_audio(path: Path) -> bool:
    if not FFMPEG:
        return False
    out = run_quiet([FFMPEG, "-hide_banner", "-i", str(path)], 30).stderr
    return bool(re.search(r"Stream #\S+.*: Audio:", out))


@app.post("/api/proxy")
async def make_proxy(request: Request):
    """An every-frame-is-a-keyframe copy of the part of a clip a rank uses, so the exporter
    can jump to any frame instantly."""
    body = await request.json()
    mid = check_id(body.get("mediaId", ""))
    meta = media_meta(mid)
    if not meta or meta["type"] != "video" or not media_file(meta).exists():
        raise HTTPException(404, "Clip not found")
    trim = max(0.0, float(body.get("trim") or 0))
    dur = max(0.1, min(60.0, float(body.get("dur") or 1)))
    ext = ".webm" if PROXY_CODEC == "vp8" else ".mp4"
    key = f"{mid}_{int(trim * 1000)}_{int(dur * 1000)}_{PROXY_CODEC}"
    out = PROXY_DIR / f"{key}{ext}"
    if not out.exists():
        PROXY_DIR.mkdir(parents=True, exist_ok=True)
        codec = (["-c:v", "libvpx", "-b:v", "8M", "-g", "1", "-deadline", "realtime", "-cpu-used", "8"] if PROXY_CODEC == "vp8"
                 else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "15", "-g", "1", "-tune", "fastdecode", "-pix_fmt", "yuv420p"])
        tmp = out.with_suffix(".part" + ext)
        cmd = [FFMPEG, "-y", "-v", "error", "-ss", f"{trim:.3f}", "-i", str(media_file(meta)), "-t", f"{dur + 0.25:.3f}", "-an",
               "-vf", "scale='min(1080,iw)':-2", *codec, str(tmp)]
        r = await asyncio.to_thread(run_quiet, cmd, 600)
        if r.returncode != 0 or not tmp.exists():
            raise HTTPException(500, "Couldn't prepare this clip for export: " + (r.stdout + r.stderr)[-300:])
        os.replace(tmp, out)
    return {"url": f"/proxy/{out.name}"}


@app.get("/proxy/{name}")
def get_proxy(name: str, request: Request):
    if not re.match(r"^[A-Za-z0-9_-]+\.(mp4|webm)$", name) or not (PROXY_DIR / name).exists():
        raise HTTPException(404, "Not found")
    return serve_with_ranges(PROXY_DIR / name, request)


@app.post("/api/export/start")
async def export_start(request: Request):
    if not FFMPEG:
        raise HTTPException(500, "ffmpeg is missing. Run the Clipdeck setup file again to restore it.")
    body = await request.json()
    fps = 60 if int(body.get("fps") or 30) == 60 else 30
    width = 1080 if int(body.get("width") or 1080) >= 1080 else 720
    eid = uuid.uuid4().hex[:10]
    tmpdir = Path(tempfile.mkdtemp(prefix="clipdeck-ex-"))
    video = tmpdir / "video.mp4"
    cmd = [FFMPEG, "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-r", str(fps),
           "-vf", f"scale={width}:-2", "-movflags", "+faststart", str(video)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=CHILD_ENV, **NOWIN)
    EXPORTS[eid] = {"proc": proc, "tmp": tmpdir, "video": video, "fps": fps, "frames": 0, "plan": body, "lock": threading.Lock()}
    return {"id": eid}


@app.post("/api/export/{eid}/frames")
async def export_frames(eid: str, request: Request):
    job = EXPORTS.get(eid)
    if not job:
        raise HTTPException(404, "This export has ended. Start it again.")
    data = await request.body()
    count = int(request.headers.get("x-frames", "1"))

    def write():
        with job["lock"]:
            job["proc"].stdin.write(data)
            job["proc"].stdin.flush()
    try:
        await asyncio.to_thread(write)
    except (BrokenPipeError, OSError):
        err = job["proc"].stderr.read().decode(errors="replace") if job["proc"].stderr else ""
        raise HTTPException(500, "The video encoder stopped: " + err[-300:])
    job["frames"] += count
    return {"frames": job["frames"]}


def build_audio_and_mux(job: dict, dest: Path) -> None:
    plan, video = job["plan"], job["video"]
    total = max(0.1, float(plan.get("total") or 0.1))
    inputs, filters, labels = [], [], []
    k = 1  # input 0 is the video
    for sc in plan.get("scenes", []):
        meta = media_meta(sc.get("mediaId") or "") if sc.get("mediaId") else None
        if not meta or meta["type"] != "video" or not media_file(meta).exists() or not has_audio(media_file(meta)):
            continue
        start, trim, dur = float(sc["start"]), float(sc.get("trim") or 0), float(sc["dur"])
        inputs += ["-ss", f"{trim:.3f}", "-t", f"{dur:.3f}", "-i", str(media_file(meta))]
        ms = int(round(start * 1000))
        filters.append(f"[{k}:a]aresample=48000,asetpts=N/SR/TB,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{k}]")
        labels.append(f"[a{k}]"); k += 1
    mu = plan.get("music") or {}
    mmeta = media_meta(mu.get("mediaId") or "") if mu.get("mediaId") else None
    if mmeta and media_file(mmeta).exists() and has_audio(media_file(mmeta)):
        vol = max(0.0, min(1.0, float(mu.get("vol", 0.6))))
        inputs += ["-stream_loop", "-1", "-i", str(media_file(mmeta))]
        filters.append(f"[{k}:a]aresample=48000,asetpts=N/SR/TB,atrim=0:{total:.3f},aformat=channel_layouts=stereo,volume={vol:.2f}[a{k}]")
        labels.append(f"[a{k}]"); k += 1
    if labels:
        mix = f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest" if len(labels) > 1 else f"{labels[0]}anull"
        # asetpts after mixing: adelay shifts timestamps, which would otherwise cut the end short.
        filters.append(f"{mix},asetpts=N/SR/TB,apad,atrim=0:{total:.3f},alimiter=limit=0.97[aout]")
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(video), *inputs, "-filter_complex", ";".join(filters),
               "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
               "-movflags", "+faststart", str(dest)]
    else:
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(video), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(dest)]
    r = run_quiet(cmd, 1800)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError("Adding the sound failed: " + (r.stdout + r.stderr)[-400:])


@app.post("/api/export/{eid}/finish")
async def export_finish(eid: str):
    job = EXPORTS.get(eid)
    if not job:
        raise HTTPException(404, "This export has ended. Start it again.")
    proc = job["proc"]

    def finish():
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        rc = proc.wait()
        if rc != 0 or not job["video"].exists():
            raise RuntimeError("Encoding the video failed: " + err[-400:])
        name = clean_filename(job["plan"].get("filename") or "ranking")
        dest = unique_path(export_dir(), name, ".mp4")
        tmp_out = job["tmp"] / "final.mp4"
        build_audio_and_mux(job, tmp_out)
        shutil.move(str(tmp_out), dest)
        return dest
    try:
        dest = await asyncio.to_thread(finish)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))
    finally:
        shutil.rmtree(job["tmp"], ignore_errors=True)
        EXPORTS.pop(eid, None)
    token = uuid.uuid4().hex[:12]
    DONE_EXPORTS[token] = dest
    return {"path": str(dest), "name": dest.name, "size": dest.stat().st_size,
            "duration": probe_duration(dest), "frames": job["frames"], "url": f"/exported/{token}"}


@app.post("/api/export/{eid}/cancel")
def export_cancel(eid: str):
    job = EXPORTS.pop(eid, None)
    if job:
        try:
            job["proc"].kill()
        except Exception:
            pass
        shutil.rmtree(job["tmp"], ignore_errors=True)
    return {"ok": True}


DONE_EXPORTS: dict[str, Path] = {}


@app.get("/exported/{token}")
def get_exported(token: str, request: Request):
    path = DONE_EXPORTS.get(token)
    if not path or not path.exists():
        raise HTTPException(404, "Not found")
    return serve_with_ranges(path, request)


@app.post("/api/open-folder")
async def open_folder(request: Request):
    body = await request.json()
    target = Path(body.get("path") or export_dir()).expanduser()
    if target.is_file():
        target = target.parent
    allowed = [export_dir().resolve(), DATA.resolve()]
    if not target.exists() or not any(target.resolve() == a or a in target.resolve().parents for a in allowed):
        raise HTTPException(400, "Can only open Clipdeck folders.")
    open_in_file_manager(target)
    return {"ok": True}


# ---------------------------------------------------------------- Claude
def _extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start < 0 or end < start:
        raise ValueError("no JSON")
    return json.loads(text[start:end + 1])


@app.post("/api/ai")
async def ask_claude(request: Request):
    body = await request.json()
    key = api_key()
    if not key:
        raise HTTPException(412, "Add your Anthropic API key in Settings to use AI features.")
    model = MODELS.get(body.get("tier", "default"), MODELS["default"])
    payload = json.dumps({
        "model": model, "max_tokens": 2500,
        "messages": [{"role": "user", "content": str(body.get("prompt", ""))[:20000]}],
    }).encode()

    def call():
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=payload, method="POST",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())

    try:
        data = await asyncio.to_thread(call)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code == 401:
            raise HTTPException(401, "Anthropic rejected the API key. Check it in Settings.")
        if e.code == 429:
            raise HTTPException(429, "Anthropic rate limit hit. Wait a moment and try again.")
        raise HTTPException(502, f"Anthropic API error {e.code}: {detail[:300]}")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Couldn't reach Anthropic: {e.reason}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if body.get("json"):
        try:
            return {"data": _extract_json(text)}
        except Exception:
            raise HTTPException(502, "Claude's answer wasn't in the expected format. Try again.")
    return {"text": text}


# ---------------------------------------------------------------- the editor
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- live updates
# When Clipdeck's own files change on disk, it updates itself: a new editor page is picked up by
# the open tab (it reloads and reopens what you were editing), and a new server restarts itself
# once nothing is downloading or exporting. New Python packages are installed first.
WATCHED = {"server": APP_DIR / "server.py", "reqs": APP_DIR / "requirements.txt", "page": STATIC / "index.html"}
UPDATE: dict = {"v": "", "error": "", "restarting": False}


def _digest(paths) -> str:
    h = hashlib.sha1()
    for p in paths:
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12]


def _busy() -> bool:
    return bool(EXPORTS) or any(j.get("state") in ("queued", "downloading", "cutting", "saving") for j in JOBS.values())


def restart_self() -> None:
    UPDATE["restarting"] = True
    flags = (0x08000000 | 0x00000008 | 0x00000200) if IS_WIN else 0  # no window, detached, own group
    subprocess.Popen([sys.executable, str(APP_DIR / "server.py"), "--no-browser", "--restarted"],
                     cwd=str(APP_DIR), env={**os.environ}, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     **({"creationflags": flags} if IS_WIN else {"start_new_session": True}))
    time.sleep(0.3)
    os._exit(0)


def watch_for_updates() -> None:
    def stamp():
        out = {}
        for k, p in WATCHED.items():
            try:
                st = p.stat()
                out[k] = (st.st_mtime_ns, st.st_size)
            except OSError:
                out[k] = None
        return out
    server_at_start = _digest([WATCHED["server"]])
    reqs_at_start = _digest([WATCHED["reqs"]])
    UPDATE["v"] = _digest(WATCHED.values())
    last = stamp()
    while True:
        time.sleep(2)
        cur = stamp()
        if cur == last:
            continue
        time.sleep(1.5)  # let a copy finish writing
        last = stamp()
        UPDATE["v"] = _digest(WATCHED.values())
        if _digest([WATCHED["server"]]) == server_at_start and _digest([WATCHED["reqs"]]) == reqs_at_start:
            UPDATE["error"] = ""
            continue  # only the page changed (or a bad change was undone): open tabs reload by themselves
        try:
            py_compile.compile(str(WATCHED["server"]), doraise=True)
        except Exception as e:  # keep running the old version rather than break
            UPDATE["error"] = f"The new version has an error, so Clipdeck kept the current one running: {e}"[:400]
            print(UPDATE["error"], flush=True)
            continue
        UPDATE["error"] = ""
        while _busy():
            time.sleep(2)
        if _digest([WATCHED["reqs"]]) != reqs_at_start:
            print("Installing new Clipdeck parts…", flush=True)
            run_quiet([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "-r", str(WATCHED["reqs"])], 900)
        print("Clipdeck updated: restarting", flush=True)
        restart_self()


# ---------------------------------------------------------------- online updates
# Clipdeck also checks its GitHub repo for a newer version. version.json there lists every app
# file with its SHA-256; files are downloaded, checked, then put in place, and the file watcher
# above takes it from there (page reload or restart).
UPDATE_REPO = os.environ.get("CLIPDECK_UPDATE_REPO", "crazychimpp/clipdeck")
UPDATE_BRANCH = os.environ.get("CLIPDECK_UPDATE_BRANCH", "main")
UPDATE_BASE = os.environ.get("CLIPDECK_UPDATE_BASE", f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}/")
APP_FILES = {"server.py", "requirements.txt", "clipdeck.ico", "static/index.html"}
ONLINE: dict = {"status": "Not checked yet", "checked": None, "latest": None, "error": "", "busy": False}


def local_version() -> dict:
    try:
        return json.loads((APP_DIR / "version.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fetch(path: str, timeout: int = 30) -> bytes:
    url = UPDATE_BASE + path + ("&" if "?" in path else "?") + f"t={int(time.time())}"
    req = urllib.request.Request(url, headers={"User-Agent": "Clipdeck-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check_online(install: bool) -> dict:
    if ONLINE["busy"]:
        return ONLINE
    ONLINE.update(busy=True, error="")
    try:
        meta = json.loads(_fetch("version.json"))
        ONLINE["latest"] = {k: meta.get(k) for k in ("version", "built", "notes")}
        mine = local_version()
        if str(meta.get("built", "")) <= str(mine.get("built", "")):
            ONLINE["status"] = "Clipdeck is up to date."
            return ONLINE
        if not install:
            ONLINE["status"] = f"Version {meta.get('version')} is available."
            return ONLINE
        files = meta.get("files") or {}
        if not files or any(p not in APP_FILES for p in files):
            raise RuntimeError("The update list looks wrong, so nothing was changed.")
        staged = {}
        for rel, sha in files.items():
            data = _fetch(rel, 120)
            if hashlib.sha256(data).hexdigest() != sha:
                raise RuntimeError(f"{rel} didn't download correctly, so nothing was changed. Clipdeck will try again later.")
            staged[rel] = data
        if "server.py" in staged:  # never install server code that can't even load
            tmp = Path(tempfile.mkdtemp(prefix="clipdeck-upd-")) / "server.py"
            tmp.write_bytes(staged["server.py"])
            py_compile.compile(str(tmp), doraise=True)
            shutil.rmtree(tmp.parent, ignore_errors=True)
        # Unchanged files are skipped; server.py goes last so a restart sees everything else first.
        for rel in sorted(staged, key=lambda r: r == "server.py"):
            dest = APP_DIR / rel
            if dest.exists() and dest.read_bytes() == staged[rel]:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            part.write_bytes(staged[rel])
            os.replace(part, dest)
        write_json(APP_DIR / "version.json", meta)
        ONLINE["status"] = f"Updated to version {meta.get('version')}."
        print(ONLINE["status"], flush=True)
    except urllib.error.HTTPError as e:
        ONLINE["error"] = "The update server isn't set up yet." if e.code == 404 else f"Update check failed (HTTP {e.code})."
    except urllib.error.URLError as e:
        ONLINE["error"] = f"Couldn't reach GitHub to check for updates ({e.reason})."
    except Exception as e:  # noqa: BLE001
        ONLINE["error"] = str(e)[:300] or "Update check failed."
    finally:
        ONLINE["checked"] = int(time.time() * 1000)
        ONLINE["busy"] = False
    return ONLINE


def online_update_loop() -> None:
    time.sleep(20)
    while True:
        if load_config().get("auto_update", True) and not _busy():
            check_online(install=True)
        time.sleep(6 * 3600)


@app.get("/api/updates")
def updates_status():
    return {**{k: v for k, v in ONLINE.items()}, "current": local_version(), "auto": load_config().get("auto_update", True),
            "repo": UPDATE_REPO}


@app.post("/api/updates/check")
async def updates_check(request: Request):
    body = await request.json()
    if "auto" in body:
        cfg = load_config(); cfg["auto_update"] = bool(body["auto"]); save_config(cfg)
    if body.get("check", True):
        await asyncio.to_thread(check_online, bool(body.get("install", True)))
    return updates_status()


@app.get("/api/version")
def version():
    return {"v": UPDATE["v"], "error": UPDATE["error"], "restarting": UPDATE["restarting"], "busy": _busy()}


def main() -> None:
    import uvicorn

    url = f"http://127.0.0.1:{PORT}"
    open_browser = "--no-browser" not in sys.argv
    restarted = "--restarted" in sys.argv

    def port_in_use() -> bool:
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", PORT)) == 0
    if restarted:  # wait for the old copy to let go of the port
        for _ in range(60):
            if not port_in_use():
                break
            time.sleep(0.25)
    if port_in_use():  # Clipdeck is already running: just bring it up
        if open_browser:
            webbrowser.open(url)
        return
    threading.Thread(target=watch_for_updates, daemon=True).start()
    threading.Thread(target=online_update_loop, daemon=True).start()
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Clipdeck running at {url}  (data: {DATA})", flush=True)
    if not IS_WIN:
        signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
