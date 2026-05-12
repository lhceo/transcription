"""FastAPI エントリポイント。

Phase 2: Google OAuth 認証を追加。
未認証ユーザーは / にアクセスすると /login にリダイレクトされる。

起動方法:
    uvicorn backend.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from backend.auth import router as auth_router
from backend.auth.dependencies import CurrentUser, _RedirectToLogin
from backend.config import load_settings
from backend.db import get_db
from backend.db.models import Transcript
from backend.transcribe import router as transcribe_router
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi import Depends
from typing import Annotated

settings = load_settings()

_BACKEND_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _BACKEND_DIR / "templates"
_STATIC_DIR = _BACKEND_DIR / "static"

app = FastAPI(
    title="Transcription Web App",
    version="0.6.0",
    description="社内向け音声文字起こし Web アプリ",
)

# セッション Cookie（署名付き）の設定。
# Cookie 属性は本番環境では Secure を強制したいので、APP_ENV で切り替える。
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="transcription_session",
    max_age=14 * 24 * 60 * 60,  # 14日（SECURITY.md の方針通り）
    same_site="lax",
    https_only=settings.is_production,
)


# 未認証時の RedirectResponse は HTTPException ではないので、
# 専用の例外ハンドラで /login にリダイレクトする。
@app.exception_handler(_RedirectToLogin)
async def _redirect_to_login_handler(
    request: Request, exc: _RedirectToLogin
) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=302)


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# 認証ルート（/login, /auth/google, /auth/google/callback, /auth/logout）
app.include_router(auth_router)

# 文字起こしルート（POST /api/transcripts, GET /api/transcripts）
app.include_router(transcribe_router)


@app.get("/health")
async def health() -> JSONResponse:
    """Railway などのヘルスチェック用エンドポイント。認証不要。"""
    return JSONResponse({"status": "ok", "version": app.version})


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """ホーム画面。認証必須。アップロード UI + 履歴一覧。"""
    stmt = (
        select(Transcript)
        .where(Transcript.user_id == user["id"])
        .where(Transcript.deleted_at.is_(None))
        .order_by(Transcript.created_at.desc())
        .limit(50)
    )
    transcripts = list(db.scalars(stmt))

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_version": app.version,
            "env": settings.env,
            "user": user,
            "transcripts": transcripts,
        },
    )
