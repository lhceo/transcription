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
from backend.auth.dependencies import AdminUser, CurrentUser, _RedirectToLogin
from backend.config import APP_VERSION, load_settings
from backend.db import get_db
from backend.db.models import Transcript, User
from backend.projects import router as projects_router
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

# プロジェクト管理ルート（/themes, /projects, /profile）
app.include_router(projects_router)


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

    users = list(db.scalars(select(User).order_by(User.last_login_at.desc())))

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


@app.get("/api/admin/recover-segments")
async def recover_segments(user: AdminUser) -> JSONResponse:
    """【一時】WAL/DB解放済みページからsegmentsを復旧する。使用後は削除すること。"""
    import struct as _struct
    import sqlite3 as _sq
    import traceback as _tb

    BAK = "/data/app.db.bak"
    DB  = "/data/app.db"

    try:
        if not Path(BAK).exists():
            return JSONResponse({"error": f"{BAK} が存在しません"}, status_code=400)

        NC = 8

        def _vi(d: bytes, p: int):
            r = 0
            for i in range(9):
                if p >= len(d):
                    return r, p
                b = d[p]; p += 1
                if i < 8:
                    r = (r << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                else:
                    r = (r << 8) | b
            return r, p

        def _gs(t: int, pl: bytes, dp: int):
            if t == 0: return None, dp
            if t == 8: return 0, dp
            if t == 9: return 1, dp
            if t == 1: return _struct.unpack_from(">b", pl, dp)[0], dp + 1
            if t == 2: return _struct.unpack_from(">h", pl, dp)[0], dp + 2
            if t == 3:
                return _struct.unpack(">I", b"\x00" + pl[dp:dp+3])[0], dp + 3
            if t == 4: return _struct.unpack_from(">i", pl, dp)[0], dp + 4
            if t == 5:
                return _struct.unpack(">Q", b"\x00\x00" + pl[dp:dp+6])[0], dp + 6
            if t == 6: return _struct.unpack_from(">q", pl, dp)[0], dp + 8
            if t == 7: return _struct.unpack_from(">d", pl, dp)[0], dp + 8
            if t >= 12 and t % 2 == 0:
                n = (t - 12) // 2
                return bytes(pl[dp:dp+n]), dp + n
            if t >= 13 and t % 2 == 1:
                n = (t - 13) // 2
                return bytes(pl[dp:dp+n]).decode("utf-8", errors="replace"), dp + n
            return None, dp

        def _pr(pl: bytes, nc: int):
            if not pl or len(pl) < 2:
                return None
            p = 0
            hs, p = _vi(pl, p)
            if hs < 1 or hs > len(pl):
                return None
            he = hs; ts: list = []; q = p
            while q < he:
                t, q = _vi(pl, q); ts.append(t)
            vs: list = []; dp = he
            for t in ts[:nc]:
                try:
                    v, dp = _gs(t, pl, dp)
                except Exception:
                    return None
                vs.append(v)
            if len(vs) < nc:
                return None
            return vs

        def _pp(data: bytes, ps: int):
            if len(data) < 8 or data[0] != 0x0D:
                return []
            nc2 = _struct.unpack_from(">H", data, 3)[0]
            if nc2 == 0 or nc2 > 500:
                return []
            rows = []
            for i in range(nc2):
                ptr = _struct.unpack_from(">H", data, 8 + i * 2)[0]
                if ptr < 8 or ptr >= ps:
                    continue
                try:
                    p = ptr
                    psz, p = _vi(data, p)
                    if psz < 1 or psz > ps * 4:
                        continue
                    rid, p = _vi(data, p)
                    pl2 = bytes(data[p:min(p + psz, len(data))])
                    row = _pr(pl2, NC)
                    if row is None:
                        continue
                    tid, oi, sl = row[0], row[1], row[4]
                    if not isinstance(tid, int): continue
                    if not isinstance(oi, int): continue
                    if not isinstance(sl, str): continue
                    if "SPEAKER" not in sl: continue
                    rows.append((rid, row))
                except Exception:
                    pass
            return rows

        # バックアップDBを直接バイナリ読み込み（sqlite_dbpage不要）
        with open(BAK, "rb") as _f:
            _hdr = _f.read(100)
        ps = _struct.unpack_from(">H", _hdr, 16)[0]
        if ps == 1:
            ps = 65536
        file_size = Path(BAK).stat().st_size
        total_pages = file_size // ps

        res: list = []
        with open(BAK, "rb") as _f:
            for pgno in range(1, total_pages + 1):
                _f.seek((pgno - 1) * ps)
                d = _f.read(ps)
                if b"SPEAKER" not in d:
                    continue
                res.extend(_pp(d, ps))

        # 重複排除（transcript_id, order_index の組）
        seen: set = set()
        dedup: list = []
        for rid, row in sorted(res):
            k = (row[0], row[1])
            if k not in seen:
                seen.add(k)
                dedup.append(row)

        if not dedup:
            return JSONResponse({
                "recovered": 0,
                "inserted": 0,
                "message": "解放済みページにセグメントデータが見つかりませんでした",
            })

        # 本番DBへ挿入
        dest = _sq.connect(DB)
        try:
            dest.execute("PRAGMA foreign_keys=OFF")
            before = dest.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
            dest.executemany(
                "INSERT INTO segments"
                "(transcript_id,order_index,start_seconds,end_seconds,"
                "speaker_label,text_content,display_name,is_edited,"
                "created_at,updated_at)"
                "VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                [
                    (
                        int(r[0]), int(r[1]),
                        float(r[2]), float(r[3]),
                        str(r[4]), str(r[5]),
                        str(r[6]) if r[6] else None,
                        int(r[7]) if r[7] else 0,
                    )
                    for r in dedup
                ],
            )
            dest.commit()
            after = dest.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
            distinct_tids = dest.execute(
                "SELECT COUNT(DISTINCT transcript_id) FROM segments"
            ).fetchone()[0]
        finally:
            dest.close()

        return JSONResponse({
            "recovered": len(dedup),
            "inserted": after - before,
            "segments_total": after,
            "transcripts_with_segments": distinct_tids,
            "page_size": ps,
            "pages_scanned": total_pages,
        })

    except Exception:
        return JSONResponse({"error": _tb.format_exc()}, status_code=500)


@app.get("/api/admin/recover-diag2")
async def recover_diag2(user: AdminUser) -> JSONResponse:
    """【一時診断2】ページ20のセル解析を詳細デバッグ。"""
    import struct as _s
    BAK = "/data/app.db.bak"

    with open(BAK, "rb") as f:
        f.seek(19 * 4096)  # page 20 (0-indexed: page 20 = offset 19*4096)
        d = f.read(4096)

    ps = 4096
    results = []

    # B-tree leaf page header
    pg_type = d[0]
    nc2 = _s.unpack_from(">H", d, 3)[0]
    cca = _s.unpack_from(">H", d, 5)[0]  # cell content area start

    header_info = {
        "page_type": hex(pg_type),
        "ncells": nc2,
        "cell_content_area_start": cca,
        "has_SPEAKER": b"SPEAKER" in d,
    }

    # varint reader
    def vi(data, p):
        r = 0
        for i in range(9):
            if p >= len(data): return r, p
            b = data[p]; p += 1
            if i < 8:
                r = (r << 7) | (b & 0x7F)
                if not (b & 0x80): break
            else:
                r = (r << 8) | b
        return r, p

    # Try first 5 cells
    for i in range(min(5, nc2)):
        ptr = _s.unpack_from(">H", d, 8 + i * 2)[0]
        cell_info: dict = {"cell_index": i, "ptr": ptr}
        if ptr < 8 or ptr >= ps:
            cell_info["skip"] = "ptr out of range"
            results.append(cell_info)
            continue
        try:
            p = ptr
            psz, p = vi(d, p)
            rid, p = vi(d, p)
            payload_start = p
            payload_end = min(p + psz, ps)
            pl = d[payload_start:payload_end]

            cell_info["psz"] = psz
            cell_info["rid"] = rid
            cell_info["payload_len_on_page"] = len(pl)
            cell_info["first20_payload_hex"] = pl[:20].hex()
            cell_info["has_SPEAKER_in_payload"] = b"SPEAKER" in pl

            # Parse header
            if pl:
                hs, hp = vi(pl, 0)
                cell_info["header_size"] = hs
                cell_info["header_size_valid"] = (1 <= hs <= len(pl))
                if 1 <= hs <= len(pl):
                    ts = []
                    q = hp
                    while q < hs:
                        t, q = vi(pl, q)
                        ts.append(t)
                    cell_info["serial_types"] = ts[:12]
                    cell_info["num_cols"] = len(ts)
        except Exception as e:
            cell_info["exception"] = str(e)
        results.append(cell_info)

    return JSONResponse({
        "page20_header": header_info,
        "cells": results,
    })


@app.get("/api/admin/recover-diag")
async def recover_diag(user: AdminUser) -> JSONResponse:
    """【一時診断】バックアップDBのページ構造を調べる。"""
    import struct as _s
    BAK = "/data/app.db.bak"
    if not Path(BAK).exists():
        return JSONResponse({"error": f"{BAK} なし"}, status_code=400)

    with open(BAK, "rb") as f:
        hdr = f.read(100)
    ps = _s.unpack_from(">H", hdr, 16)[0]
    if ps == 1:
        ps = 65536
    file_size = Path(BAK).stat().st_size
    total_pages = file_size // ps

    type_counts: dict = {}
    spk_type_counts: dict = {}
    spk_samples: list = []

    with open(BAK, "rb") as f:
        for pgno in range(1, total_pages + 1):
            f.seek((pgno - 1) * ps)
            d = f.read(ps)
            pt = d[0]
            type_counts[pt] = type_counts.get(pt, 0) + 1
            if b"SPEAKER" in d:
                spk_type_counts[pt] = spk_type_counts.get(pt, 0) + 1
                if len(spk_samples) < 8:
                    nc2 = _s.unpack_from(">H", d, 3)[0] if len(d) >= 5 else -1
                    spk_samples.append({
                        "pgno": pgno,
                        "type": hex(pt),
                        "ncells": nc2,
                        "spk0_count": d.count(b"SPEAKER_0"),
                        "first16": d[:16].hex(),
                    })

    return JSONResponse({
        "ps": ps,
        "total_pages": total_pages,
        "page_types": {hex(k): v for k, v in sorted(type_counts.items())},
        "speaker_page_types": {hex(k): v for k, v in sorted(spk_type_counts.items())},
        "speaker_page_samples": spk_samples,
    })


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
