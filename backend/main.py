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
    """【一時】\x07\x07アンカースキャンでsegmentsを復旧する。使用後は削除すること。"""
    import struct as _struct
    import sqlite3 as _sq
    import traceback as _tb

    BAK = "/data/app.db.bak"
    DB  = "/data/app.db"

    try:
        if not Path(BAK).exists():
            return JSONResponse({"error": f"{BAK} が存在しません"}, status_code=400)

        def _vi(d: bytes, p: int):
            r = 0
            for i in range(9):
                if p >= len(d): return r, p
                b = d[p]; p += 1
                if i < 8:
                    r = (r << 7) | (b & 0x7F)
                    if not (b & 0x80): break
                else: r = (r << 8) | b
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

        def _parse_seg(pl: bytes):
            """
            セグメントレコードとして pl を解析して返す。
            ペイロード列構造（11列）:
              col[0]=id(NULL), col[1]=transcript_id, col[2]=order_index,
              col[3]=start_seconds(float), col[4]=end_seconds(float),
              col[5]=speaker_label, col[6]=text_content,
              col[7]=display_name, col[8]=is_edited
            id は INTEGER PRIMARY KEY のため payload では NULL(type=0)。
            """
            if len(pl) < 12: return None
            p = 0
            hs, p = _vi(pl, p)
            if hs < 4 or hs > len(pl) or hs > 120: return None
            he = hs
            ts: list = []; q = p
            while q < he:
                t, q = _vi(pl, q)
                ts.append(t)
                if len(ts) > 15: return None
            # id(NULL)+tid+oi+ss+es+sl+text+dn+ie = 最低9列
            if len(ts) < 9: return None
            # ts[0]=NULL(id), ts[3]=7(ss), ts[4]=7(es)
            if ts[0] != 0: return None
            if ts[3] != 7 or ts[4] != 7: return None
            vs: list = []; dp = he
            for t in ts[:9]:
                try:
                    v, dp = _gs(t, pl, dp)
                except Exception:
                    return None
                vs.append(v)
            if len(vs) < 9: return None
            tid, oi, ss, es, sl = vs[1], vs[2], vs[3], vs[4], vs[5]
            if not isinstance(tid, int) or tid < 1: return None
            if not isinstance(oi, int) or oi < 0: return None
            if not isinstance(ss, float) or not isinstance(es, float): return None
            if ss < 0 or es < ss or es > 86400 * 7: return None
            if not isinstance(sl, str) or "SPEAKER" not in sl: return None
            return vs

        # デバッグカウンタ（問題特定後に削除）
        _dbg: dict = {
            "x0707_in_wal": 0,
            "wal_frames": 0,
            "parse_ok": 0,
            "parse_fail_hs": 0,
            "parse_fail_ts": 0,
            "parse_fail_val": 0,
        }

        def _scan_bytes(data: bytes, seen: set, dedup: list) -> None:
            pos = 0
            while True:
                k = data.find(b"\x07\x07", pos)
                if k == -1: break
                _dbg["x0707_in_wal"] += 1
                for off in range(3, 8):
                    ps_start = k - off
                    if ps_start < 0: continue
                    row = _parse_seg(data[ps_start:])
                    if row is not None:
                        _dbg["parse_ok"] += 1
                        key = (row[1], row[2])
                        if key not in seen:
                            seen.add(key)
                            dedup.append(row)
                        break
                else:
                    _dbg["parse_fail_val"] += 1
                pos = k + 1

        with open(BAK, "rb") as _f:
            _hdr = _f.read(100)
        ps = _struct.unpack_from(">H", _hdr, 16)[0]
        if ps == 1: ps = 65536
        file_size = Path(BAK).stat().st_size
        total_pages = file_size // ps

        seen: set = set()
        dedup: list = []

        # ① DB バックアップをスキャン
        with open(BAK, "rb") as _f:
            for pgno in range(1, total_pages + 1):
                _f.seek((pgno - 1) * ps)
                d = _f.read(ps)
                _scan_bytes(d, seen, dedup)

        # ② WAL バックアップをスキャン（フレームのページデータのみ）
        WAL = "/data/app.db-wal.bak"
        if Path(WAL).exists():
            with open(WAL, "rb") as _f:
                wal_raw = _f.read()
            _dbg["wal_size"] = len(wal_raw)
            if len(wal_raw) >= 32:
                _mag = _struct.unpack(">I", wal_raw[:4])[0]
                _e2 = ">" if _mag == 0x377f0682 else "<"
                _wps = _struct.unpack(f"{_e2}I", wal_raw[8:12])[0]
                _fsz = 24 + _wps
                _dbg["wal_ps"] = _wps
                _dbg["wal_fsz"] = _fsz
                _wp = 32
                while _wp + _fsz <= len(wal_raw):
                    _pd = wal_raw[_wp + 24: _wp + _fsz]
                    _scan_bytes(_pd, seen, dedup)
                    _wp += _fsz
                    _dbg["wal_frames"] += 1

        if not dedup:
            return JSONResponse({
                "recovered": 0,
                "inserted": 0,
                "message": "セグメントデータが見つかりませんでした",
                "debug": _dbg,
            })

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
                        # vs[0]=id(NULL) はスキップ; vs[1..8] を使用
                        int(r[1]), int(r[2]),
                        float(r[3]), float(r[4]),
                        str(r[5]), str(r[6]),
                        str(r[7]) if r[7] else None,
                        int(r[8]) if r[8] else 0,
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


@app.get("/api/admin/export-recovered")
async def export_recovered(user: AdminUser):
    """【一時】復旧済みセグメントを transcript_id ごとにまとめてZIPダウンロード。"""
    import sqlite3 as _sq
    import zipfile as _zf
    import io as _io
    from fastapi.responses import StreamingResponse

    DB = "/data/app.db"
    conn = _sq.connect(DB)
    try:
        # transcript_id 一覧
        tids = [r[0] for r in conn.execute(
            "SELECT DISTINCT transcript_id FROM segments ORDER BY transcript_id"
        ).fetchall()]

        buf = _io.BytesIO()
        with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as zf:
            for tid in tids:
                rows = conn.execute(
                    "SELECT order_index, start_seconds, end_seconds, "
                    "speaker_label, display_name, text_content "
                    "FROM segments WHERE transcript_id=? "
                    "ORDER BY order_index",
                    (tid,)
                ).fetchall()

                lines = [f"=== transcript_id={tid} ({len(rows)}セグメント) ===\n"]
                for oi, ss, es, sl, dn, text in rows:
                    def fmt(sec):
                        h = int(sec) // 3600
                        m = (int(sec) % 3600) // 60
                        s = int(sec) % 60
                        return f"{h:02d}:{m:02d}:{s:02d}"
                    name = dn if dn else sl
                    lines.append(
                        f"[{fmt(ss or 0)} - {fmt(es or 0)}] {name}\n"
                        f"{text or ''}\n"
                    )

                content = "\n".join(lines)
                zf.writestr(f"transcript_{tid:04d}.txt", content)

        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=recovered_segments.zip"
            },
        )
    finally:
        conn.close()


@app.get("/api/admin/recover-diag5")
async def recover_diag5(user: AdminUser) -> JSONResponse:
    """【一時診断5】frame_wp=45352 のページを手動ステップ実行で確認。"""
    import struct as _s
    import traceback as _tb
    WAL = "/data/app.db-wal.bak"
    try:
        with open(WAL, "rb") as f:
            wal_data = f.read()

        # frame at wp=45352
        wp = 45352
        pd = wal_data[wp + 24 : wp + 4120]  # 4096 bytes

        # 1. \x07\x07 を探す
        k = pd.find(b"\x07\x07")
        if k == -1:
            return JSONResponse({"error": "\\x07\\x07 not found in frame"})

        # 2. off=4 で payload 先頭を特定
        ps_start = k - 4
        pl = pd[ps_start:]

        # 3. header_size を読む
        hs_byte = pl[0]

        # 4. varint で hs を読む
        def vi(d, p):
            r = 0
            for i in range(9):
                if p >= len(d): return r, p
                b = d[p]; p += 1
                if i < 8:
                    r = (r << 7) | (b & 0x7F)
                    if not (b & 0x80): break
                else: r = (r << 8) | b
            return r, p

        hs, p1 = vi(pl, 0)
        he = hs

        # 5. serial types を読む
        ts = []
        q = p1
        while q < he:
            t, q = vi(pl, q)
            ts.append(t)
            if len(ts) > 15:
                break

        # 6. data area からいくつか読む
        def gs(t, buf, dp):
            if t == 0: return None, dp
            if t == 8: return 0, dp
            if t == 9: return 1, dp
            if t == 1: return _s.unpack_from(">b", buf, dp)[0], dp+1
            if t == 2: return _s.unpack_from(">h", buf, dp)[0], dp+2
            if t == 3:
                return _s.unpack(">I", b"\x00"+buf[dp:dp+3])[0], dp+3
            if t == 4: return _s.unpack_from(">i", buf, dp)[0], dp+4
            if t == 5:
                return _s.unpack(">Q", b"\x00\x00"+buf[dp:dp+6])[0], dp+6
            if t == 6: return _s.unpack_from(">q", buf, dp)[0], dp+8
            if t == 7: return _s.unpack_from(">d", buf, dp)[0], dp+8
            if t >= 12 and t%2==0:
                n=(t-12)//2; return bytes(buf[dp:dp+n]), dp+n
            if t >= 13 and t%2==1:
                n=(t-13)//2
                return bytes(buf[dp:dp+n]).decode("utf-8",errors="replace"), dp+n
            return None, dp

        vals = []
        dp = he
        for t in ts[:9]:
            try:
                v, dp = gs(t, pl, dp)
                vals.append(repr(v)[:60])
            except Exception as e:
                vals.append(f"ERROR:{e}")
                break

        # check results
        checks = {
            "k": k,
            "ps_start": ps_start,
            "hs_byte_hex": hex(hs_byte),
            "hs": hs,
            "ts": ts,
            "len_ts": len(ts),
            "ts[0]": ts[0] if ts else None,
            "ts[3]": ts[3] if len(ts) > 3 else None,
            "ts[4]": ts[4] if len(ts) > 4 else None,
            "check_ts0_ok": ts[0] == 0 if ts else False,
            "check_ts34_ok": (len(ts) > 4 and ts[3] == 7 and ts[4] == 7),
            "vals": vals,
        }
        if len(vals) >= 6:
            sl_repr = vals[5]
            checks["sl_has_SPEAKER"] = "SPEAKER" in sl_repr

        return JSONResponse(checks)
    except Exception:
        return JSONResponse({"error": _tb.format_exc()}, status_code=500)


@app.get("/api/admin/recover-diag4")
async def recover_diag4(user: AdminUser) -> JSONResponse:
    """【一時診断4】WAL内SPEAKER_0前後の生バイトを表示。"""
    import struct as _s
    WAL = "/data/app.db-wal.bak"
    if not Path(WAL).exists():
        return JSONResponse({"error": "WALなし"}, status_code=400)
    with open(WAL, "rb") as f:
        wal_data = f.read()
    magic = _s.unpack(">I", wal_data[:4])[0]
    e = ">" if magic == 0x377f0682 else "<"
    wal_ps = _s.unpack(f"{e}I", wal_data[8:12])[0]
    frame_sz = 24 + wal_ps

    results = []
    wp = 32
    while wp + frame_sz <= len(wal_data) and len(results) < 3:
        fh = wal_data[wp:wp+24]
        pg_no = _s.unpack(f"{e}I", fh[:4])[0]
        pd = wal_data[wp+24:wp+frame_sz]
        # SPEAKER_0 が含まれるフレームだけ
        idx = pd.find(b"SPEAKER_0")
        if idx != -1:
            # SPEAKER_0 の前後60バイトをhexで
            lo = max(0, idx - 60)
            hi = min(len(pd), idx + 30)
            ctx = pd[lo:hi]
            # 最も近い \x07\x07 の位置（前方）
            x77_before = -1
            for back in range(1, 80):
                if idx - back >= 1 and pd[idx-back] == 0x07 and pd[idx-back-1] == 0x07:
                    x77_before = idx - back - 1
                    break
            results.append({
                "frame_wp": wp,
                "page_no": pg_no,
                "speaker0_at": idx,
                "x0707_before_at": x77_before,
                "dist_x0707_to_speaker0": idx - x77_before if x77_before >= 0 else None,
                "context_lo": lo,
                "hex": ctx.hex(),
                "ascii": "".join(
                    chr(b) if 0x20 <= b < 0x7f else "." for b in ctx
                ),
            })
        wp += frame_sz

    # 追加: WAL全体の最初のSPEAKER_0位置を特定
    first_global = wal_data.find(b"SPEAKER_0")
    return JSONResponse({
        "wal_ps": wal_ps,
        "frame_sz": frame_sz,
        "first_speaker0_global_offset": first_global,
        "samples": results,
    })


@app.get("/api/admin/recover-diag3")
async def recover_diag3(user: AdminUser) -> JSONResponse:
    """【一時診断3】SPEAKERフィルタなしで全ページ+WALをスキャン。"""
    import struct as _s
    import traceback as _tb
    BAK = "/data/app.db.bak"
    WAL = "/data/app.db-wal.bak"

    try:
        def _vi(d: bytes, p: int):
            r = 0
            for i in range(9):
                if p >= len(d): return r, p
                b = d[p]; p += 1
                if i < 8:
                    r = (r << 7) | (b & 0x7F)
                    if not (b & 0x80): break
                else: r = (r << 8) | b
            return r, p

        def _gs(t, pl, dp):
            if t == 0: return None, dp
            if t == 8: return 0, dp
            if t == 9: return 1, dp
            if t == 1: return _s.unpack_from(">b", pl, dp)[0], dp+1
            if t == 2: return _s.unpack_from(">h", pl, dp)[0], dp+2
            if t == 3:
                return _s.unpack(">I", b"\x00"+pl[dp:dp+3])[0], dp+3
            if t == 4: return _s.unpack_from(">i", pl, dp)[0], dp+4
            if t == 5:
                return _s.unpack(">Q", b"\x00\x00"+pl[dp:dp+6])[0], dp+6
            if t == 6: return _s.unpack_from(">q", pl, dp)[0], dp+8
            if t == 7: return _s.unpack_from(">d", pl, dp)[0], dp+8
            if t >= 12 and t%2==0:
                n=(t-12)//2; return bytes(pl[dp:dp+n]), dp+n
            if t >= 13 and t%2==1:
                n=(t-13)//2
                return bytes(pl[dp:dp+n]).decode("utf-8",errors="replace"), dp+n
            return None, dp

        def _try_seg(pl: bytes):
            if len(pl) < 12: return None
            p = 0
            hs, p = _vi(pl, p)
            if hs < 4 or hs > len(pl) or hs > 100: return None
            he = hs; ts = []; q = p
            while q < he:
                t, q = _vi(pl, q); ts.append(t)
                if len(ts) > 15: return None
            if len(ts) < 10: return None
            if ts[2] != 7 or ts[3] != 7: return None
            vs = []; dp = he
            for t in ts[:8]:
                try: v, dp = _gs(t, pl, dp)
                except: return None
                vs.append(v)
            if len(vs) < 8: return None
            tid, oi, ss, es = vs[0], vs[1], vs[2], vs[3]
            if not isinstance(tid, int) or tid < 1: return None
            if not isinstance(oi, int) or oi < 0: return None
            if not isinstance(ss, float) or not isinstance(es, float): return None
            if ss < 0 or es < ss or es > 86400*7: return None
            sl = vs[4]
            return {"tid": tid, "oi": oi, "ss": round(ss,2),
                    "es": round(es,2), "sl": str(sl)[:30]}

        # ── DBバックアップをフルスキャン（SPEAKERフィルタなし）──
        with open(BAK, "rb") as f:
            hdr = f.read(100)
        ps = _s.unpack_from(">H", hdr, 16)[0]
        if ps == 1: ps = 65536
        file_size = Path(BAK).stat().st_size
        total_pages = file_size // ps

        db_hits = 0
        db_samples: list = []
        seen: set = set()

        with open(BAK, "rb") as f:
            for pgno in range(1, total_pages+1):
                f.seek((pgno-1)*ps)
                d = f.read(ps)
                pos = 0
                while True:
                    k = d.find(b"\x07\x07", pos)
                    if k == -1: break
                    for off in range(2, 10):
                        ps2 = k - off
                        if ps2 < 0: continue
                        row = _try_seg(d[ps2:])
                        if row is not None:
                            db_hits += 1
                            key = (row["tid"], row["oi"])
                            if key not in seen:
                                seen.add(key)
                                if len(db_samples) < 5:
                                    db_samples.append(
                                        {"pgno": pgno, "off": off, **row}
                                    )
                            break
                    pos = k + 1

        # ── WALファイルのチェック ──
        wal_info: dict = {}
        wal_hits = 0
        wal_samples: list = []

        if Path(WAL).exists():
            wal_size = Path(WAL).stat().st_size
            wal_info["exists"] = True
            wal_info["size"] = wal_size
            with open(WAL, "rb") as f:
                wal_data = f.read()
            wal_info["speaker0_count"] = wal_data.count(b"SPEAKER_0")
            wal_info["x0707_count"] = wal_data.count(b"\x07\x07")

            # WALフレームをスキャン
            if wal_size >= 32:
                magic = _s.unpack(">I", wal_data[:4])[0]
                e = ">" if magic == 0x377f0682 else "<"
                wal_ps = _s.unpack(f"{e}I", wal_data[8:12])[0]
                frame_sz = 24 + wal_ps
                wp = 32
                wal_seen: set = set()
                while wp + frame_sz <= wal_size:
                    pd = wal_data[wp+24:wp+frame_sz]
                    pos2 = 0
                    while True:
                        k2 = pd.find(b"\x07\x07", pos2)
                        if k2 == -1: break
                        for off2 in range(2, 10):
                            ps3 = k2 - off2
                            if ps3 < 0: continue
                            row2 = _try_seg(pd[ps3:])
                            if row2 is not None:
                                wal_hits += 1
                                key2 = (row2["tid"], row2["oi"])
                                if key2 not in wal_seen:
                                    wal_seen.add(key2)
                                    if len(wal_samples) < 5:
                                        wal_samples.append(row2)
                                break
                        pos2 = k2 + 1
                    wp += frame_sz
        else:
            wal_info["exists"] = False

        return JSONResponse({
            "db_pages": total_pages,
            "db_hits_total": db_hits,
            "db_unique_segments": len(seen),
            "db_samples": db_samples,
            "wal": wal_info,
            "wal_hits_total": wal_hits,
            "wal_unique_segments": len(wal_seen) if Path(WAL).exists() else 0,
            "wal_samples": wal_samples,
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
