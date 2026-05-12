"""FastAPI エントリポイント。

Phase 1 では「ブラウザで開ける状態」を作るのみ。
認証・アップロード・AssemblyAI 連携は後続フェーズで追加していく。

起動方法:
    uvicorn backend.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.config import load_settings

settings = load_settings()

_BACKEND_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _BACKEND_DIR / "templates"
_STATIC_DIR = _BACKEND_DIR / "static"

app = FastAPI(
    title="Transcription Web App",
    version="0.2.0",
    description="社内向け音声文字起こし Web アプリ",
)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


@app.get("/health")
async def health() -> JSONResponse:
    """Railway などのヘルスチェック用エンドポイント。認証不要。"""
    return JSONResponse({"status": "ok", "version": app.version})


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """ホーム画面（Phase 1 では認証なし、雛形だけ表示）。"""
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_version": app.version,
            "env": settings.env,
        },
    )
