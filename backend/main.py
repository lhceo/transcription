"""FastAPI エントリポイント。

Phase 2: Google OAuth 認証を追加。
未認証ユーザーは / にアクセスすると /login にリダイレクトされる。

起動方法:
    uvicorn backend.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from backend.auth import router as auth_router
from backend.auth.dependencies import CurrentUser, _RedirectToLogin
from backend.config import APP_VERSION, load_settings
from backend.db import get_db
from backend.db.models import Transcript
from backend.transcribe import router as transcribe_router
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi import Depends
from typing import Annotated

logger = logging.getLogger(__name__)

settings = load_settings()

_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent
_TEMPLATES_DIR = _BACKEND_DIR / "templates"
_STATIC_DIR = _BACKEND_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時に Alembic マイグレーションを実行する。

    Railway 等の永続ボリュームは pre-deploy ステップではマウントされず
    Start フェーズで初めて利用可能になるため、マイグレーションはここで走らせる。
    """
    alembic_ini = _REPO_ROOT / "alembic.ini"
    logger.info("Alembic マイグレーション開始: %s", alembic_ini)
    cfg = AlembicConfig(str(alembic_ini))
    command.upgrade(cfg, "head")
    logger.info("Alembic マイグレーション完了")
    yield


app = FastAPI(
    title="Transcription Web App",
    version=APP_VERSION,
    description="社内向け音声文字起こし Web アプリ",
    lifespan=lifespan,
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
# ただし /api/* は AJAX 経由なので、302 では fetch() が /login を取得して
# 200 を返してしまい「保存できているように見えて実は失敗」が起きる。
# API パスでは 401 JSON を返し、フロント側で再ログイン誘導する。
@app.exception_handler(_RedirectToLogin)
async def _redirect_to_login_handler(
    request: Request, exc: _RedirectToLogin
) -> JSONResponse | RedirectResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content={
                "code": "UNAUTHENTICATED",
                "message": "セッションが切れました。ページを再読み込みしてログインしてください。",
            },
        )
    return RedirectResponse(url="/login", status_code=302)


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

from backend.transcribe.eta import compute_eta_text  # noqa: E402
templates.env.globals["eta_text"] = compute_eta_text

from backend.transcribe.cost import current_month_cost_yen  # noqa: E402
from backend.transcribe.display import has_stored_audio, transcript_display_name  # noqa: E402
templates.env.globals["display_name"] = transcript_display_name
templates.env.globals["has_audio"] = has_stored_audio

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

    monthly_cost = current_month_cost_yen(db)
    monthly_limit = settings.monthly_cost_limit_yen

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_version": app.version,
            "env": settings.env,
            "user": user,
            "transcripts": transcripts,
            "monthly_cost": monthly_cost,
            "monthly_limit": monthly_limit,
        },
    )
