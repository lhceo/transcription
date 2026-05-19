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
    """起動時の初期化と、バックグラウンドタスクの起動/停止。

    1. Alembic マイグレーション (Railway 永続ボリュームは Start フェーズで
       初めてマウントされるためここで実行)
    2. 自動削除バックグラウンドタスクの起動 (v1.0.2)
    """
    # 1. マイグレーション
    alembic_ini = _REPO_ROOT / "alembic.ini"
    logger.info("Alembic マイグレーション開始: %s", alembic_ini)
    cfg = AlembicConfig(str(alembic_ini))
    command.upgrade(cfg, "head")
    logger.info("Alembic マイグレーション完了")

    # 2. 自動削除タスクを起動 (5 分後に初回スキャン、その後 24 時間ごと)
    import asyncio
    from backend.transcribe.retention import retention_loop

    retention_task = asyncio.create_task(retention_loop())
    logger.info("自動削除バックグラウンドタスク起動")

    try:
        yield
    finally:
        # シャットダウン時にタスクをキャンセル
        retention_task.cancel()
        try:
            await retention_task
        except asyncio.CancelledError:
            pass
        logger.info("自動削除バックグラウンドタスク停止")


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

from backend.transcribe.cost import get_cost_summary  # noqa: E402
from backend.transcribe.display import has_stored_audio, transcript_display_name  # noqa: E402
from backend.transcribe.retention import expiry_status  # noqa: E402
from backend.transcribe.storage_usage import get_summary as get_storage_summary  # noqa: E402
templates.env.globals["display_name"] = transcript_display_name
templates.env.globals["has_audio"] = has_stored_audio
templates.env.globals["expiry_status"] = expiry_status
templates.env.globals["storage_usage"] = get_storage_summary
templates.env.globals["cost_usage"] = get_cost_summary

# 認証ルート（/login, /auth/google, /auth/google/callback, /auth/logout）
app.include_router(auth_router)

# 文字起こしルート（POST /api/transcripts, GET /api/transcripts）
app.include_router(transcribe_router)


@app.get("/health")
async def health() -> JSONResponse:
    """Railway などのヘルスチェック用エンドポイント。認証不要。"""
    return JSONResponse({"status": "ok", "version": app.version})


@app.get("/api/admin/disk-status")
async def disk_status(user: CurrentUser) -> JSONResponse:
    """ディスク使用量の診断。管理者確認用。"""
    import shutil
    from backend.db.session import _DB_PATH, DATABASE_URL

    def dir_size(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"exists": False, "path": path}
        try:
            total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            count = sum(1 for f in p.rglob("*") if f.is_file())
            return {"exists": True, "path": path, "bytes": total, "files": count,
                    "pretty": f"{total / 1_000_000:.1f} MB"}
        except Exception as e:
            return {"exists": True, "path": path, "error": str(e)}

    def file_info(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"exists": False, "path": path}
        try:
            return {"exists": True, "path": path, "bytes": p.stat().st_size,
                    "pretty": f"{p.stat().st_size / 1_000_000:.2f} MB"}
        except Exception as e:
            return {"exists": True, "path": path, "error": str(e)}

    def disk_free(path: str) -> dict:
        try:
            stat = shutil.disk_usage(path)
            return {
                "path": path,
                "total_gb": round(stat.total / 1e9, 2),
                "used_gb": round(stat.used / 1e9, 2),
                "free_gb": round(stat.free / 1e9, 2),
                "used_pct": round(stat.used * 100 / stat.total, 1),
            }
        except Exception as e:
            return {"path": path, "error": str(e)}

    # 音声ファイル一覧（transcriptID別サイズ）
    from backend.transcribe.storage_usage import _AUDIO_DIR
    audio_files = []
    if _AUDIO_DIR.exists():
        for f in sorted(_AUDIO_DIR.iterdir()):
            if f.is_file():
                audio_files.append({
                    "filename": f.name,
                    "bytes": f.stat().st_size,
                    "pretty": f"{f.stat().st_size / 1_000_000:.1f} MB",
                })

    db_path = str(_DB_PATH)
    return JSONResponse({
        "database_url": DATABASE_URL,
        "db_file": file_info(db_path),
        "db_wal": file_info(db_path + "-wal"),
        "db_shm": file_info(db_path + "-shm"),
        "data_dir": dir_size("/data"),
        "audio_files": audio_files,
        "disk_data": disk_free("/data"),
        "disk_app": disk_free("/app"),
    })


@app.post("/api/admin/free-audio-space")
async def free_audio_space(user: CurrentUser) -> JSONResponse:
    """緊急: ディスクフル時に音声ファイルをDBへの書き込みなしで削除する。

    DBが書き込み不能な状態でもこのエンドポイントは動作する。
    ファイルを削除後、通常の削除操作が可能になる。
    """
    from backend.transcribe.storage_usage import _AUDIO_DIR
    deleted = []
    errors = []
    freed_bytes = 0
    if _AUDIO_DIR.exists():
        for f in sorted(_AUDIO_DIR.iterdir()):
            if f.is_file():
                size = f.stat().st_size
                try:
                    f.unlink()
                    deleted.append({"filename": f.name, "bytes": size})
                    freed_bytes += size
                    logger.info("緊急領域解放: 音声削除 %s (%d bytes)", f.name, size)
                except Exception as e:
                    errors.append({"filename": f.name, "error": str(e)})
    return JSONResponse({
        "deleted": deleted,
        "errors": errors,
        "freed_bytes": freed_bytes,
        "freed_pretty": f"{freed_bytes / 1_000_000:.1f} MB",
    })


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """ホーム画面。認証必須。アップロード UI + 履歴一覧。

    v1.0.2 以降、月次コストはヘッダーの cost インジケーター (base.html)
    で表示するためここでは渡さない。ストレージ使用量も同様にヘッダー側で
    取得する。
    """
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


@app.get("/help", response_class=HTMLResponse)
async def help_page(
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    """使い方ガイド (ヘルプ画面)。ログイン必須。"""
    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "app_version": app.version,
            "env": settings.env,
            "user": user,
        },
    )
