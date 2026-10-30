import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlencode

import requests
import yt_dlp
from yt_dlp.utils import DownloadError

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, HttpUrl
from google.oauth2 import service_account, credentials as user_credentials
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
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "/etc/secrets/youtube-cookies.txt")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "1TMEIFXjm_SBQT4wSq549XW_AJXQtpFKr")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "https://wenfetch-ytdlp-api.onrender.com/auth/google/callback")
OAUTH_STATES = {}

app = FastAPI(title="WenFetch yt-dlp API", version="2.1.0")
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
    title = re.sub(r'[\\\\/:*?"<>|\\x00-\\x1f]', "_", title).strip(" .")
    return (title or "video")[:150]


def get_drive_credentials():
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    if client_id and client_secret and refresh_token:
        creds = user_credentials.Credentials(token=None, refresh_token=refresh_token, token_uri="https://oauth2.googleapis.com/token", client_id=client_id, client_secret=client_secret, scopes=["https://www.googleapis.com/auth/drive"])
        from google.auth.transport.requests import Request
        if not creds.valid:
            creds.refresh(Request())
        return creds
    if Path(GOOGLE_CREDENTIALS_FILE).exists():
        return service_account.Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=["https://www.googleapis.com/auth/drive"])
    raise RuntimeError("OAuth do Google Drive não está configurado no Render.")


def upload_to_google_drive(file_path: Path) -> dict:
    if not GOOGLE_DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID não foi configurado.")
    credentials = get_drive_credentials()
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
    JOBS[job_id].update(status="processing", message="Preparando download direto com yt-dlp.")
    try:
        def progress_hook(data):
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes", 0)
                if total:
                    percent = min(99, int(downloaded * 100 / total))
                    JOBS[job_id].update(message=f"Baixando com yt-dlp: {percent}%")
                else:
                    JOBS[job_id].update(message="Baixando vídeo com yt-dlp.")
            elif data.get("status") == "finished":
                JOBS[job_id].update(message="Download recebido. Preparando arquivo.")

        if fmt == "mp3":
            format_selector = "bestaudio/best"
        else:
            height = int(fmt.replace("p", ""))
            format_selector = (
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}][ext=mp4]/best[height<={height}]/best"
            )

        ydl_options = {
            "format": format_selector,
            "outtmpl": str(job_dir / "%(title).150B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
            "continuedl": True,
            "max_filesize": MAX_FILE_SIZE,
            "merge_output_format": "mp4",
            "progress_hooks": [progress_hook],
            "socket_timeout": 30,
        }
        cookies_path = Path(YOUTUBE_COOKIES_FILE)
        if cookies_path.is_file():
            ydl_options["cookiefile"] = str(cookies_path)
        if fmt == "mp3":
            ydl_options["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        JOBS[job_id].update(message="Conectando ao YouTube com yt-dlp.")
        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(url, download=True)
            prepared = Path(ydl.prepare_filename(info))
        if fmt == "mp3":
            destination = prepared.with_suffix(".mp3")
        else:
            destination = prepared.with_suffix(".mp4") if prepared.with_suffix(".mp4").exists() else prepared
        if not destination.exists():
            candidates = [p for p in job_dir.iterdir() if p.is_file()]
            if not candidates:
                raise RuntimeError("yt-dlp terminou sem produzir um arquivo.")
            destination = max(candidates, key=lambda p: p.stat().st_size)

        if destination.stat().st_size == 0:
            raise RuntimeError("O YouTube não entregou um arquivo válido.")
        if destination.stat().st_size > MAX_FILE_SIZE:
            raise RuntimeError("O arquivo excede o limite de 1,5 GB.")

        JOBS[job_id].update(message="Enviando o arquivo para o Google Drive.")
        drive_info = None
        drive_error = None
        try:
            drive_info = upload_to_google_drive(destination)
        except Exception as upload_exc:
            drive_error = (str(upload_exc).strip() or upload_exc.__class__.__name__)[:400]

        update = {
            "status": "completed",
            "filename": destination.name,
            "size": destination.stat().st_size,
            "message": "Download concluído com yt-dlp e enviado ao Google Drive." if drive_info else "Download concluído, mas não foi possível enviar ao Google Drive.",
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
        if isinstance(exc, DownloadError):
            message = f"yt-dlp não conseguiu baixar este vídeo: {message}"
        JOBS[job_id].update(status="failed", error=message[:500], completed_at=time.time())

@app.get("/auth/google/start")
def google_oauth_start():
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    if not client_id:
        raise HTTPException(status_code=503, detail="Configure GOOGLE_OAUTH_CLIENT_ID no Render.")
    state = uuid.uuid4().hex
    OAUTH_STATES[state] = time.time()
    params = {"client_id": client_id, "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI, "response_type": "code", "scope": "https://www.googleapis.com/auth/drive", "access_type": "offline", "prompt": "consent", "state": state}
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@app.get("/auth/google/callback", response_class=HTMLResponse)
def google_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        raise HTTPException(status_code=400, detail="Autorização Google cancelada: " + error)
    created = OAUTH_STATES.pop(state, None)
    if not created or time.time() - created > 900:
        raise HTTPException(status_code=400, detail="Estado OAuth inválido ou expirado. Inicie novamente.")
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="Configure Client ID e Client Secret no Render.")
    token_response = requests.post("https://oauth2.googleapis.com/token", data={"code": code, "client_id": client_id, "client_secret": client_secret, "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI, "grant_type": "authorization_code"}, timeout=30)
    if not token_response.ok:
        raise HTTPException(status_code=502, detail="Falha ao trocar código OAuth por token. Confira as credenciais e o URI de redirecionamento.")
    refresh_token = token_response.json().get("refresh_token", "")
    if not refresh_token:
        raise HTTPException(status_code=400, detail="O Google não retornou refresh token. Revogue o acesso anterior do WenFetch e autorize novamente.")
    return HTMLResponse("<h2>Autorização concluída</h2><p>Copie o token abaixo e salve no Render como GOOGLE_OAUTH_REFRESH_TOKEN. Não compartilhe este token.</p><textarea rows=5 cols=100 readonly>" + refresh_token + "</textarea>")

@app.get("/")
def root():
    return {"service": "WenFetch yt-dlp API", "status": "ok"}

@app.get("/health")
def health():
    return {"status": "ok", "provider": "yt-dlp", "configured": True}

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
