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
from pytubefix import YouTube
from pytubefix.cli import on_progress
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

APP_DIR = Path("/tmp/wenfetch")
APP_DIR.mkdir(parents=True, exist_ok=True)
JOBS = {}
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "21600"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1"))
semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
MAX_FILE_SIZE = 1_500_000_000
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "/etc/secrets/google-drive-credentials.json")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "1TMEIFXjm_SBQT4wSq549XW_AJXQtpFKr")

app = FastAPI(title="WenFetch pytubefix API", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://wenfetch.vercel.app", "http://localhost:3000", "http://127.0.0.1:3000"],
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
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be",
    }:
        raise HTTPException(status_code=400, detail="Por enquanto, aceitamos apenas links do YouTube.")
    return url


def safe_title(title: str) -> str:
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")
    return (title or "video")[:150]


def upload_to_google_drive(file_path: Path) -> dict:
    if not Path(GOOGLE_CREDENTIALS_FILE).exists():
        raise RuntimeError("Credencial do Google Drive não encontrada no Render.")
    if not GOOGLE_DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID não foi configurado.")
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    metadata = {"name": file_path.name, "parents": [GOOGLE_DRIVE_FOLDER_ID]}
    media = MediaFileUpload(str(file_path), resumable=True)
    uploaded = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()
    return {
        "id": uploaded["id"],
        "url": uploaded.get("webViewLink", f"https://drive.google.com/file/d/{uploaded['id']}/view"),
    }


def download_job(job_id: str, url: str, fmt: str):
    job_dir = APP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    JOBS[job_id].update(status="processing", message="Conectando ao YouTube e preparando o download.")
    try:
        yt = YouTube(url, on_progress_callback=on_progress, use_oauth=False)
        title = safe_title(yt.title)
        if fmt == "mp3":
            stream = yt.streams.filter(only_audio=True).order_by("abr").desc().first()
            if stream is None:
                raise RuntimeError("Não foi encontrado um fluxo de áudio disponível para este vídeo.")
            ext = stream.subtype or "mp4"
            downloaded = Path(stream.download(output_path=str(job_dir), filename=f"{title}.{ext}"))
            if downloaded.stat().st_size > MAX_FILE_SIZE:
                raise RuntimeError("O arquivo excede o limite de 1,5 GB.")
            if ext != "mp3":
                import subprocess
                mp3_path = job_dir / f"{title}.mp3"
                proc = subprocess.run(["ffmpeg", "-y", "-i", str(downloaded), "-vn", "-b:a", "192k", str(mp3_path)], capture_output=True, text=True, timeout=7200)
                if proc.returncode != 0 or not mp3_path.exists():
                    raise RuntimeError("Não foi possível converter o áudio para MP3.")
                downloaded.unlink(missing_ok=True)
                downloaded = mp3_path
        else:
            height = int(fmt[:-1])
            stream = yt.streams.filter(progressive=True, file_extension="mp4", res=f"{height}p").first()
            if stream is None:
                stream = yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()
                if stream is not None:
                    available_height = int(re.search(r"(\\d+)p", stream.resolution).group(1))
                    candidates = yt.streams.filter(progressive=True, file_extension="mp4").filter(res=f"{min(height, available_height)}p")
                    stream = candidates.first() or stream
            if stream is None:
                raise RuntimeError("Não há um formato MP4 progressivo compatível disponível para este vídeo.")
            if stream.filesize and stream.filesize > MAX_FILE_SIZE:
                raise RuntimeError("O arquivo excede o limite de 1,5 GB.")
            downloaded = Path(stream.download(output_path=str(job_dir), filename=f"{title}.mp4"))
        if not downloaded.exists() or downloaded.stat().st_size == 0:
            raise RuntimeError("O download terminou sem gerar um arquivo.")
        drive_info = None
        drive_error = None
        try:
            drive_info = upload_to_google_drive(downloaded)
        except Exception as upload_exc:
            drive_error = (str(upload_exc).strip() or upload_exc.__class__.__name__)[:400]
        update = {
            "status": "completed",
            "filename": downloaded.name,
            "size": downloaded.stat().st_size,
            "message": "Download concluído e arquivo enviado ao Google Drive." if drive_info else "Download concluído, mas não foi possível enviar ao Google Drive.",
            "completed_at": time.time(),
        }
        if drive_info:
            update["drive_url"] = drive_info["url"]
            update["drive_file_id"] = drive_info["id"]
        if drive_error:
            update["drive_error"] = drive_error
        JOBS[job_id].update(**update)
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        message = str(exc).strip() or exc.__class__.__name__
        JOBS[job_id].update(status="failed", error=message[:500], completed_at=time.time())

@app.get("/")
def root():
    return {"service": "WenFetch pytubefix API", "status": "ok"}

@app.get("/health")
def health():
    return {"status": "ok", "library": "pytubefix"}

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
        if job.get("drive_url"):
            result["drive_url"] = job["drive_url"]
        if job.get("drive_error"):
            result["drive_error"] = job["drive_error"]
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
