import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
import yt_dlp

APP_DIR = Path("/tmp/wenfetch")
APP_DIR.mkdir(parents=True, exist_ok=True)
JOBS = {}
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "21600"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1"))
semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

app = FastAPI(title="WenFetch yt-dlp API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://wenfetch.vercel.app",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class DownloadRequest(BaseModel):
    url: HttpUrl
    format: str = "360p"


def validate_youtube_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in {
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "music.youtube.com", "youtu.be", "www.youtu.be",
    }:
        raise HTTPException(status_code=400, detail="Por enquanto, aceitamos apenas links do YouTube.")
    return url


def download_job(job_id: str, url: str, fmt: str):
    job_dir = APP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    JOBS[job_id].update(status="processing", message="Baixando e preparando o arquivo.")
    if fmt == "mp3":
        output_template = str(job_dir / "%(title).150B.%(ext)s")
        opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "max_filesize": 1_500_000_000,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        }
    else:
        height = int(fmt[:-1])
        output_template = str(job_dir / "%(title).150B.%(ext)s")
        opts = {
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "max_filesize": 1_500_000_000,
        }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [p for p in job_dir.iterdir() if p.is_file() and p.stat().st_size > 0]
        if not files:
            raise RuntimeError("O download terminou sem gerar um arquivo.")
        result = max(files, key=lambda p: p.stat().st_size)
        JOBS[job_id].update(
            status="completed",
            filename=result.name,
            size=result.stat().st_size,
            message="Arquivo pronto para baixar.",
            completed_at=time.time(),
        )
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        message = str(exc)
        if len(message) > 500:
            message = message[:500] + "…"
        JOBS[job_id].update(status="failed", error=message, completed_at=time.time())


@app.get("/")
def root():
    return {"service": "WenFetch yt-dlp API", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok", "yt_dlp_version": yt_dlp.version.__version__}


@app.post("/api/download", status_code=202)
async def create_download(payload: DownloadRequest):
    url = validate_youtube_url(str(payload.url))
    fmt = payload.format.lower()
    if fmt not in {"mp3", "360p", "720p", "1080p"}:
        raise HTTPException(status_code=400, detail="Formato não suportado.")
    if semaphore.locked():
        raise HTTPException(status_code=429, detail="O servidor já está processando um download. Tente novamente em instantes.")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"id": job_id, "status": "queued", "created_at": time.time(), "format": fmt}
    asyncio.create_task(run_download(job_id, url, fmt))
    return {"id": job_id, "status": "queued"}


async def run_download(job_id: str, url: str, fmt: str):
    async with semaphore:
        await asyncio.to_thread(download_job, job_id, url, fmt)


@app.get("/api/status")
def get_status(id: str):
    job = JOBS.get(id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabalho não encontrado ou expirado. Envie o link novamente.")
    result = {k: v for k, v in job.items() if k not in {"created_at", "completed_at", "filename"}}
    if job.get("status") == "completed":
        result["download_url"] = f"/api/file/{id}"
    return result


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "completed":
        raise HTTPException(status_code=404, detail="Arquivo ainda não está pronto ou já expirou.")
    path = APP_DIR / job_id / job["filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="O arquivo expirou. Envie o link novamente.")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.on_event("startup")
async def cleanup_loop():
    async def cleanup():
        while True:
            now = time.time()
            for job_id, job in list(JOBS.items()):
                if now - job.get("created_at", now) > JOB_TTL_SECONDS:
                    shutil.rmtree(APP_DIR / job_id, ignore_errors=True)
                    JOBS.pop(job_id, None)
            await asyncio.sleep(300)
    asyncio.create_task(cleanup())
