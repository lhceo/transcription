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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from backend.auth import router as auth_router
from backend.auth.dependencies import AdminUser, CurrentUser, _RedirectToLogin
from backend.config import APP_VERSION, load_settings
from backend.db import get_db
from backend.db.models import SpeakerHistory, Transcript, User
from backend.projects import router as projects_router
from backend.transcribe import router as transcribe_router
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi import Depends
from typing import Annotated

logger = logging.getLogger(__name__)

settings = load_settings()

_BACKEND_DIR = Path(__file__).resolve().parent
_MAINTENANCE_FLAG = Path("/data/maintenance.flag")
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
    # 0. 直接 sqlite3 でスキーマ修復 (alembic が誤ったDBに当たっても最低限保証)
    import sqlite3 as _sq3
    import os as _os
    _db_url = _os.environ.get("DATABASE_URL", "")
    _db_path = _db_url.replace("sqlite:///", "") if _db_url.startswith("sqlite:") else None
    if _db_path:
        logger.info("スキーマ修復開始: %s", _db_path)
        try:
            _co = _sq3.connect(_db_path)
            _cu = _co.cursor()
            for _sql in [
                "ALTER TABLE transcripts ADD COLUMN meeting_date DATETIME",
                "ALTER TABLE transcripts ADD COLUMN meeting_location VARCHAR(200)",
                "ALTER TABLE transcripts ADD COLUMN meeting_purpose TEXT",
                "ALTER TABLE transcripts ADD COLUMN meeting_agenda TEXT",
                "ALTER TABLE transcripts ADD COLUMN last_polished_at DATETIME",
                "ALTER TABLE transcripts ADD COLUMN meeting_participants TEXT",
                "ALTER TABLE users ADD COLUMN last_seen_at DATETIME",
                "ALTER TABLE project_vocabulary ADD COLUMN meaning TEXT",
            ]:
                try:
                    _cu.execute(_sql)
                except Exception:
                    pass
            _cu.execute(
                "CREATE TABLE IF NOT EXISTS transcript_shares("
                "id INTEGER NOT NULL,"
                "transcript_id INTEGER NOT NULL,"
                "shared_with_user_id INTEGER NOT NULL,"
                "created_at DATETIME NOT NULL,"
                "PRIMARY KEY(id),"
                "CONSTRAINT uq_transcript_share "
                "UNIQUE(transcript_id,shared_with_user_id),"
                "FOREIGN KEY(transcript_id) "
                "REFERENCES transcripts(id) ON DELETE CASCADE,"
                "FOREIGN KEY(shared_with_user_id) "
                "REFERENCES users(id) ON DELETE CASCADE)"
            )
            try:
                _cu.execute(
                    "CREATE INDEX ix_transcript_shares_user "
                    "ON transcript_shares(shared_with_user_id)"
                )
            except Exception:
                pass
            _cu.execute(
                "CREATE TABLE IF NOT EXISTS people("
                "id INTEGER NOT NULL,"
                "owner_user_id INTEGER NOT NULL,"
                "name VARCHAR(100) NOT NULL,"
                "company VARCHAR(200),"
                "job_title VARCHAR(200),"
                "role VARCHAR(200),"
                "created_at DATETIME NOT NULL,"
                "PRIMARY KEY(id),"
                "CONSTRAINT uq_people_owner_name "
                "UNIQUE(owner_user_id,name),"
                "FOREIGN KEY(owner_user_id) "
                "REFERENCES users(id) ON DELETE CASCADE)"
            )
            try:
                _cu.execute("ALTER TABLE people ADD COLUMN role VARCHAR(200)")
            except Exception:
                pass
            _cu.execute("UPDATE alembic_version SET version_num='c2d4e6f8a0b1'")
            _co.commit()
            _co.execute("PRAGMA wal_checkpoint(PASSIVE)")
            _co.close()
            logger.info("スキーマ修復完了")
        except Exception as _e:
            logger.warning("スキーマ修復失敗: %s", _e)

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

    # 3. WAL チェックポイント (10分ごと)
    # SQLite WAL を main DB に書き込み済みにすることで、
    # 万一のプロセスクラッシュ時のデータ損失ゼロを目指す。
    async def _wal_checkpoint_loop():
        from backend.db.session import _DB_PATH
        import sqlite3
        await asyncio.sleep(60)
        while True:
            try:
                conn = sqlite3.connect(str(_DB_PATH))
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.close()
                logger.debug("WAL チェックポイント完了")
            except Exception as e:
                logger.warning("WAL チェックポイント失敗: %s", e)
            await asyncio.sleep(600)

    wal_task = asyncio.create_task(_wal_checkpoint_loop())
    logger.info("WAL チェックポイントタスク起動")

    try:
        yield
    finally:
        retention_task.cancel()
        wal_task.cancel()
        for task in (retention_task, wal_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("バックグラウンドタスク停止")


_MAINTENANCE_BYPASS_PREFIXES = (
    "/health",
    "/admin",
    "/api/admin",
    "/login",
    "/auth",
    "/static",
)


class MaintenanceMiddleware(BaseHTTPMiddleware):
    """メンテナンスフラグファイルが存在する間、管理者以外に 503 を返す。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not any(path.startswith(p) for p in _MAINTENANCE_BYPASS_PREFIXES):
            if _MAINTENANCE_FLAG.exists():
                # 管理者はメンテナンス中も全ページにアクセス可能
                user = request.session.get("user") if hasattr(request, "session") else None
                is_admin = (
                    isinstance(user, dict)
                    and user.get("email", "").lower() == settings.admin_email
                )
                if not is_admin:
                    html = _BACKEND_DIR / "templates" / "maintenance.html"
                    body = html.read_text(encoding="utf-8") if html.exists() else "<h1>メンテナンス中</h1>"
                    return HTMLResponse(content=body, status_code=503)
        return await call_next(request)


app = FastAPI(
    title="Transcription Web App",
    version=APP_VERSION,
    description="社内向け音声文字起こし Web アプリ",
    lifespan=lifespan,
)

# メンテナンスミドルウェア（SessionMiddleware より先に追加 = リクエスト処理の先頭で動く）
app.add_middleware(MaintenanceMiddleware)

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
from backend.release_notes import get_release_notes  # noqa: E402
templates.env.globals["eta_text"] = compute_eta_text
templates.env.globals["release_notes"] = get_release_notes(APP_VERSION)

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

# プロジェクト管理ルート（/themes, /projects, /profile）
app.include_router(projects_router)


@app.post("/api/heartbeat")
async def heartbeat(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """ログイン中ユーザーの最終アクティブ時刻を更新する。フロントが60秒ごとに呼ぶ。"""
    from datetime import datetime, timezone
    u = db.get(User, user["id"])
    if u:
        u.last_seen_at = datetime.now(timezone.utc)
        db.commit()
    return JSONResponse({"ok": True})


@app.get("/health")
async def health() -> JSONResponse:
    """Railway などのヘルスチェック用エンドポイント。認証不要。"""
    return JSONResponse({"status": "ok", "version": app.version})


@app.get("/api/admin/users-stats")
async def admin_users_stats(
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """管理者向け: 全ユーザーの利用状況サマリー。"""
    from backend.transcribe.storage_usage import _AUDIO_DIR
    from backend.transcribe.cost import month_start_utc, estimate_cost_yen
    from backend.transcribe.retention import _ensure_utc_aware

    try:
        users = list(db.scalars(select(User).order_by(User.last_login_at.desc())))
    except Exception as e:
        logger.error("users-stats: DB クエリ失敗 %s", e)
        return JSONResponse({"users": [], "db_error": str(e)}, status_code=200)

    audio_sizes: dict[int, int] = {}
    if _AUDIO_DIR.exists():
        for f in _AUDIO_DIR.iterdir():
            if f.is_file():
                try:
                    tid = int(f.stem)
                    audio_sizes[tid] = f.stat().st_size
                except ValueError:
                    pass

    month_start = month_start_utc()
    result = []
    for u in users:
        active_transcripts = [t for t in u.transcripts if t.deleted_at is None]
        audio_bytes = sum(audio_sizes.get(t.id, 0) for t in active_transcripts)
        last_upload = max((t.created_at for t in active_transcripts), default=None)

        cost_this_month = 0
        for t in active_transcripts:
            if t.created_at is None:
                continue
            if _ensure_utc_aware(t.created_at) < month_start:
                continue
            if t.status == "completed":
                cost_this_month += int(t.cost_yen or 0)
            elif t.status in ("uploaded", "processing"):
                cost_this_month += estimate_cost_yen(t.audio_duration_seconds, t.model_tier or "best")

        result.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "picture": u.picture_url,
            "transcript_count": len(active_transcripts),
            "audio_bytes": audio_bytes,
            "audio_pretty": f"{audio_bytes / 1_000_000:.1f} MB" if audio_bytes > 0 else "0 MB",
            "cost_this_month_yen": cost_this_month,
            "cost_this_month_pretty": f"¥{cost_this_month:,}" if cost_this_month > 0 else "¥0",
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "last_upload_at": last_upload.isoformat() if last_upload else None,
            "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
        })

    return JSONResponse({"users": result})


@app.get("/api/admin/disk-status")
async def disk_status(user: AdminUser) -> JSONResponse:
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
async def free_audio_space(user: AdminUser) -> JSONResponse:
    """緊急: ディスクフル時に音声ファイルをDBへの書き込みなしで削除する。"""
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


@app.get("/api/admin/maintenance/status")
async def maintenance_status(user: AdminUser) -> JSONResponse:
    """メンテナンスモードの現在の状態を返す。"""
    active = _MAINTENANCE_FLAG.exists()
    return JSONResponse({"active": active})


@app.post("/api/admin/maintenance/start")
async def maintenance_start(user: AdminUser) -> JSONResponse:
    """メンテナンスモードを開始する（フラグファイルを作成）。"""
    _MAINTENANCE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    _MAINTENANCE_FLAG.touch()
    logger.info("メンテナンスモード開始: %s", user["email"])
    return JSONResponse({"active": True})


@app.post("/api/admin/maintenance/stop")
async def maintenance_stop(user: AdminUser) -> JSONResponse:
    """メンテナンスモードを終了する（フラグファイルを削除）。"""
    if _MAINTENANCE_FLAG.exists():
        _MAINTENANCE_FLAG.unlink()
    logger.info("メンテナンスモード終了: %s", user["email"])
    return JSONResponse({"active": False})


@app.delete("/api/admin/speaker-history")
async def clear_speaker_history(
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
    user_id: int | None = None,
) -> JSONResponse:
    """管理者専用: 話者候補の履歴を削除する。
    user_id 指定 → そのユーザーのみ削除。
    user_id 未指定 → 管理者以外の全ユーザーの履歴を削除。
    """
    settings = load_settings()
    if user_id is not None:
        target = db.get(User, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
        count = db.query(SpeakerHistory).filter(SpeakerHistory.user_id == user_id).delete()
    else:
        # 管理者ユーザーの ID を除外
        admin_ids = [u.id for u in db.scalars(select(User).where(User.email == settings.admin_email))]
        q = db.query(SpeakerHistory)
        if admin_ids:
            q = q.filter(SpeakerHistory.user_id.notin_(admin_ids))
        count = q.delete()
    db.commit()
    logger.info("話者履歴クリア by %s: %d 件削除 (target_user_id=%s)", user["email"], count, user_id)
    return JSONResponse({"deleted": count})


@app.get("/admin/users/{target_user_id}", response_class=HTMLResponse)
async def admin_user_detail(
    request: Request,
    target_user_id: int,
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """管理者専用: 指定ユーザーの文字起こし一覧ページ。"""
    target = db.get(User, target_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    return templates.TemplateResponse(
        request,
        "admin_user.html",
        {
            "app_version": app.version,
            "env": settings.env,
            "user": user,
            "is_admin": True,
            "target_user": {
                "id": target.id,
                "name": target.name,
                "email": target.email,
                "picture": target.picture_url,
            },
        },
    )


@app.get("/api/admin/users/{target_user_id}/transcripts")
async def admin_user_transcripts(
    target_user_id: int,
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """管理者専用: 指定ユーザーの文字起こし一覧 (削除済み含む)。"""
    from backend.transcribe.storage_usage import _AUDIO_DIR
    from backend.transcribe.display import transcript_display_name

    target = db.get(User, target_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")

    rows = list(
        db.scalars(
            select(Transcript)
            .where(Transcript.user_id == target_user_id)
            .order_by(Transcript.created_at.desc())
        )
    )

    def audio_exists(tid: int) -> bool:
        if _AUDIO_DIR.exists():
            for f in _AUDIO_DIR.iterdir():
                if f.stem == str(tid):
                    return True
        return False

    result = []
    for t in rows:
        result.append({
            "id": t.id,
            "title": transcript_display_name(t),
            "status": t.status,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
            "duration_sec": t.audio_duration_seconds,
            "file_size_bytes": t.file_size_bytes,
            "has_audio": audio_exists(t.id),
        })

    return JSONResponse({"transcripts": result, "user": {"name": target.name, "email": target.email}})


@app.delete("/api/admin/transcripts/{transcript_id}/audio")
async def admin_delete_audio(
    transcript_id: int,
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """管理者専用: 音声ファイルのみ削除（テキスト・セグメントは残す）。"""
    from backend.transcribe.storage import delete_stored_audio

    transcript = db.get(Transcript, transcript_id)
    if transcript is None:
        raise HTTPException(status_code=404, detail="文字起こしが見つかりません")

    deleted = delete_stored_audio(transcript_id)
    logger.info("管理者 %s が transcript %d の音声を削除", user["email"], transcript_id)
    return JSONResponse({"deleted": deleted})


@app.delete("/api/admin/transcripts/{transcript_id}")
async def admin_delete_transcript(
    transcript_id: int,
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """管理者専用: 文字起こし全削除（音声＋ソフト削除）。"""
    from backend.transcribe.storage import delete_stored_audio
    from datetime import datetime, timezone

    transcript = db.get(Transcript, transcript_id)
    if transcript is None:
        raise HTTPException(status_code=404, detail="文字起こしが見つかりません")

    transcript.deleted_at = datetime.now(timezone.utc)
    db.commit()
    delete_stored_audio(transcript_id)
    logger.info("管理者 %s が transcript %d を全削除", user["email"], transcript_id)
    return JSONResponse({"deleted": True})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user: AdminUser) -> HTMLResponse:
    """管理ページ。管理者のみ。"""
    import shutil
    from backend.transcribe.storage_usage import get_summary

    try:
        du = shutil.disk_usage("/data")
        disk_info = {
            "ok": True,
            "total_gb": f"{du.total / 1e9:.2f}",
            "used_gb": f"{du.used / 1e9:.2f}",
            "free_gb": f"{du.free / 1e9:.2f}",
            "percent": int(du.used * 100 / du.total),
        }
    except Exception as e:
        disk_info = {"ok": False, "error": str(e)}

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "app_version": app.version,
            "env": settings.env,
            "user": user,
            "is_admin": True,
            "disk_info": disk_info,
            "storage": get_summary(),
        },
    )


@app.post("/api/admin/free-audio-space-redirect", response_class=HTMLResponse)
async def free_audio_space_redirect(request: Request, user: AdminUser) -> HTMLResponse:
    """フォーム POST から呼ばれる版（ブラウザリダイレクト付き）。"""
    from backend.transcribe.storage_usage import _AUDIO_DIR
    import shutil
    freed_bytes = 0
    deleted_count = 0
    if _AUDIO_DIR.exists():
        for f in sorted(_AUDIO_DIR.iterdir()):
            if f.is_file():
                size = f.stat().st_size
                try:
                    f.unlink()
                    freed_bytes += size
                    deleted_count += 1
                    logger.info("緊急領域解放: 音声削除 %s (%d bytes)", f.name, size)
                except Exception as e:
                    logger.warning("音声削除失敗: %s %s", f.name, e)
    try:
        du = shutil.disk_usage("/data")
        disk_info = f"合計 {du.total/1e9:.2f} GB / 使用 {du.used/1e9:.2f} GB / 空き {du.free/1e9:.2f} GB ({du.used*100//du.total}%)"
    except Exception:
        disk_info = "取得失敗"
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><title>削除完了</title>
<style>body{{font-family:sans-serif;max-width:600px;margin:40px auto;padding:0 20px}}
.ok{{background:#f0fff4;border:1px solid #9ae6b4;padding:16px;border-radius:8px}}</style></head>
<body>
<h1>削除完了</h1>
<div class="ok">
<p>✅ {deleted_count} 件の音声ファイルを削除しました（{freed_bytes/1_000_000:.1f} MB 解放）</p>
<p><strong>現在のディスク:</strong> {disk_info}</p>
</div>
<p>これで新しいファイルをアップロードできるようになりました。</p>
<p><a href="/">← ホームに戻る</a></p>
</body></html>"""
    return HTMLResponse(html)


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

    from backend.transcribe.storage_usage import get_effective_max_upload_bytes
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_version": app.version,
            "env": settings.env,
            "user": user,
            "is_admin": user["email"].lower() == settings.admin_email,
            "transcripts": transcripts,
            "max_upload_bytes": get_effective_max_upload_bytes(),
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
            "is_admin": user["email"].lower() == settings.admin_email,
        },
    )
