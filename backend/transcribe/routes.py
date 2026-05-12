"""文字起こし関連の HTTP ルート。

Phase 3 ではアップロード受付と履歴表示までを実装する。
AssemblyAI への送信・結果取得は Phase 4 で追加する。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.dependencies import CurrentUser
from backend.config import load_settings
from backend.db import get_db
from backend.db.models import Transcript
from backend.transcribe.constants import ALLOWED_EXTENSIONS, MAX_UPLOAD_BYTES
from backend.transcribe.storage import UploadTooLargeError, save_upload_to_tmp
from backend.transcribe.tasks import process_transcript

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter()


@router.post("/api/transcripts", status_code=status.HTTP_201_CREATED)
async def create_transcript(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    model_tier: str = Form("best"),
) -> HTMLResponse:
    """音声ファイルをアップロードし、ジョブを作成する。

    Phase 3 ではここで処理は止まる（AssemblyAI 送信は Phase 4）。
    レスポンスは HTMX の swap 用 HTML 断片。
    """
    # ──── 入力バリデーション ────────────────────────────────────────────────
    if model_tier not in {"best", "nano"}:
        raise HTTPException(
            status_code=400, detail={"code": "INVALID_MODEL_TIER", "message": "モデル指定が不正です"}
        )

    if not file.filename:
        raise HTTPException(
            status_code=400, detail={"code": "INVALID_FILE", "message": "ファイル名がありません"}
        )

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "INVALID_FILE_FORMAT",
                "message": "対応していないファイル形式です。mp3 か mp4 をご利用ください。",
            },
        )

    # ──── ファイル保存 ─────────────────────────────────────────────────────
    try:
        save_path, size_bytes = await save_upload_to_tmp(file, max_bytes=MAX_UPLOAD_BYTES)
    except UploadTooLargeError:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "FILE_TOO_LARGE",
                "message": "ファイルサイズが 2GB を超えています。",
            },
        )

    # ──── DB レコード作成 ──────────────────────────────────────────────────
    # AssemblyAI 未設定の時は uploaded（処理されない）、設定済なら processing
    settings = load_settings()
    initial_status = "processing" if settings.has_assemblyai else "uploaded"

    transcript = Transcript(
        user_id=user["id"],
        original_filename=file.filename,
        file_size_bytes=size_bytes,
        status=initial_status,
        model_tier=model_tier,
        language="ja",
        created_at=datetime.now(timezone.utc),
    )
    db.add(transcript)
    db.commit()
    db.refresh(transcript)

    logger.info(
        "Transcript レコード作成: id=%s user_id=%s filename=%s size=%s tier=%s status=%s",
        transcript.id,
        user["id"],
        file.filename,
        size_bytes,
        model_tier,
        initial_status,
    )

    # ──── バックグラウンドで文字起こしを開始 ───────────────────────────
    if settings.has_assemblyai:
        # asyncio.create_task で fire-and-forget。
        # レスポンスが返った後も event loop 上で動き続ける。
        asyncio.create_task(process_transcript(transcript.id, save_path))
    else:
        logger.warning(
            "ASSEMBLYAI_API_KEY 未設定のため、ジョブ %s は uploaded 状態のままです",
            transcript.id,
        )

    # ──── レスポンス（HTMX で履歴行を append する HTML 断片） ───────────
    return templates.TemplateResponse(
        request,
        "_transcript_row.html",
        {"transcript": transcript},
    )


@router.get("/api/transcripts", response_class=HTMLResponse)
async def list_transcripts(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """履歴一覧を返す（HTMX で部分更新するための HTML）。"""
    stmt = (
        select(Transcript)
        .where(Transcript.user_id == user["id"])
        .where(Transcript.deleted_at.is_(None))
        .order_by(Transcript.created_at.desc())
        .limit(100)
    )
    transcripts = list(db.scalars(stmt))

    return templates.TemplateResponse(
        request,
        "_transcript_list.html",
        {"transcripts": transcripts},
    )


@router.get("/api/transcripts/{transcript_id}/row", response_class=HTMLResponse)
async def get_transcript_row(
    request: Request,
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """1行分の HTML を返す（HTMX で processing 行を polling 更新するため）。

    HTMX 側で hx-get="/api/transcripts/{id}/row" hx-trigger="every 5s" を使い、
    自分自身の li を入れ替える。
    """
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or transcript.user_id != user["id"]:
        # 「存在しない」と「権限がない」を区別しない（SECURITY.md 方針）
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    return templates.TemplateResponse(
        request,
        "_transcript_row.html",
        {"transcript": transcript},
    )


@router.get("/api/transcripts/{transcript_id}/status")
async def get_transcript_status(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """軽量なステータス確認用 JSON エンドポイント。"""
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or transcript.user_id != user["id"]:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    return JSONResponse(
        {
            "id": transcript.id,
            "status": transcript.status,
            "audio_duration_seconds": transcript.audio_duration_seconds,
            "cost_yen": transcript.cost_yen,
            "error_message": transcript.error_message,
            "completed_at": (
                transcript.completed_at.isoformat() if transcript.completed_at else None
            ),
        }
    )
