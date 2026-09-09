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
from pydantic import BaseModel
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.auth.dependencies import CurrentUser
from backend.config import APP_VERSION, load_settings
from backend.db import get_db
from backend.db.models import Person, ProjectVocabulary, Segment, Speaker, SpeakerHistory, Transcript, TranscriptShare, User
from backend.transcribe.constants import ALLOWED_EXTENSIONS, MAX_UPLOAD_BYTES, MEDIA_TYPES
from backend.transcribe.cost import (
    get_cost_summary,
    next_month_start_jst_text,
    will_exceed_limit,
)
from backend.transcribe.eta import compute_eta_text
from backend.transcribe.display import has_stored_audio, transcript_display_name
from backend.transcribe.retention import expiry_status
from backend.transcribe.storage import (
    UploadTooLargeError,
    delete_stored_audio,
    find_stored_audio,
    save_upload_to_tmp,
)
from backend.transcribe.storage_usage import (
    get_summary as get_storage_summary,
    would_exceed_hard_limit,
)
from backend.transcribe.tasks import process_transcript

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.globals["eta_text"] = compute_eta_text
templates.env.globals["display_name"] = transcript_display_name
templates.env.globals["has_audio"] = has_stored_audio
templates.env.globals["expiry_status"] = expiry_status
templates.env.globals["storage_usage"] = get_storage_summary
templates.env.globals["cost_usage"] = get_cost_summary

router = APIRouter()


# ── アクセス制御ヘルパー ────────────────────────────────────────────────────

def _is_owner(transcript: Transcript, user_id: int) -> bool:
    return transcript.user_id == user_id


def _can_access(transcript: Transcript, user_id: int, db: Session) -> bool:
    """オーナーまたは共有されたユーザーならアクセス可。"""
    if _is_owner(transcript, user_id):
        return True
    return db.scalar(
        select(TranscriptShare).where(
            TranscriptShare.transcript_id == transcript.id,
            TranscriptShare.shared_with_user_id == user_id,
        )
    ) is not None


class ShareBody(BaseModel):
    shared_with_user_id: int


# ── 共有 API ─────────────────────────────────────────────────────────────────

@router.get("/api/transcripts/{transcript_id}/shares")
async def list_shares(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """共有先ユーザー一覧を返す（オーナーのみ）。"""
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or not _is_owner(transcript, user["id"]) or transcript.deleted_at is not None:
        raise HTTPException(status_code=404)
    shares = db.scalars(
        select(TranscriptShare)
        .where(TranscriptShare.transcript_id == transcript_id)
    ).all()
    return JSONResponse([
        {"user_id": s.shared_with_user_id, "name": s.shared_with.name, "email": s.shared_with.email}
        for s in shares
    ])


@router.post("/api/transcripts/{transcript_id}/shares", status_code=201)
async def add_share(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: ShareBody,
) -> JSONResponse:
    """共有先を追加する（オーナーのみ）。"""
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or not _is_owner(transcript, user["id"]) or transcript.deleted_at is not None:
        raise HTTPException(status_code=404)
    if body.shared_with_user_id == user["id"]:
        raise HTTPException(status_code=400, detail="自分自身には共有できません")
    target = db.get(User, body.shared_with_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    existing = db.scalar(
        select(TranscriptShare).where(
            TranscriptShare.transcript_id == transcript_id,
            TranscriptShare.shared_with_user_id == body.shared_with_user_id,
        )
    )
    if existing:
        return JSONResponse({"ok": True, "already": True})
    share = TranscriptShare(
        transcript_id=transcript_id,
        shared_with_user_id=body.shared_with_user_id,
    )
    db.add(share)
    db.commit()
    return JSONResponse({"ok": True, "name": target.name}, status_code=201)


@router.delete("/api/transcripts/{transcript_id}/shares/{target_user_id}")
async def remove_share(
    transcript_id: int,
    target_user_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """共有を解除する（オーナーのみ）。"""
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or not _is_owner(transcript, user["id"]) or transcript.deleted_at is not None:
        raise HTTPException(status_code=404)
    share = db.scalar(
        select(TranscriptShare).where(
            TranscriptShare.transcript_id == transcript_id,
            TranscriptShare.shared_with_user_id == target_user_id,
        )
    )
    if share:
        db.delete(share)
        db.commit()
    return JSONResponse({"ok": True})


@router.get("/api/users")
async def list_users(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """共有先選択用のユーザー一覧（自分以外）。"""
    users = db.scalars(select(User).where(User.id != user["id"]).order_by(User.name)).all()
    return JSONResponse([{"id": u.id, "name": u.name, "email": u.email} for u in users])


@router.post("/api/transcripts", status_code=status.HTTP_201_CREATED)
async def create_transcript(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    model_tier: str = Form("best"),
    audio_duration_seconds: str | None = Form(None),
    speakers_expected: str | None = Form(None),
    word_boost: str | None = Form(None),
    project_id: str | None = Form(None),
    meeting_date: str | None = Form(None),
    meeting_location: str | None = Form(None),
    meeting_purpose: str | None = Form(None),
    meeting_agenda: str | None = Form(None),
) -> HTMLResponse:
    """音声ファイルをアップロードし、ジョブを作成する。

    Phase 3 ではここで処理は止まる（AssemblyAI 送信は Phase 4）。
    レスポンスは HTMX の swap 用 HTML 断片。

    `audio_duration_seconds` はクライアント側で `<audio>`/`<video>` の
    metadata から読み取った概算値（任意）。処理時間目安の表示に使う。
    AssemblyAI 完了時に正確な値で上書きされる。
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
                "message": (
                    "対応していないファイル形式です。"
                    "mp3 / mp4 / m4a / wav / mov をご利用ください。"
                ),
            },
        )

    # クライアント側で読み取った音声長を解釈する（信頼境界の外なので
    # 現実的な範囲 0 < x <= 6時間 にクランプ）。コスト上限チェックと
    # DB 保存の両方で使う。
    parsed_duration: float | None = None
    if audio_duration_seconds:
        try:
            d = float(audio_duration_seconds)
            if 0 < d <= 6 * 3600:
                parsed_duration = d
        except (TypeError, ValueError):
            pass

    parsed_speakers: int | None = None
    if speakers_expected:
        try:
            n = int(speakers_expected)
            if 2 <= n <= 10:
                parsed_speakers = n
        except (TypeError, ValueError):
            pass

    user_word_boost: list[str] = []
    if word_boost and word_boost.strip():
        user_word_boost = [w.strip() for w in word_boost.splitlines() if w.strip()]

    parsed_project_id: int | None = None
    if project_id:
        try:
            parsed_project_id = int(project_id)
        except (TypeError, ValueError):
            pass

    # ──── 月次コスト上限チェック ────────────────────────────────────────────
    settings = load_settings()
    if settings.monthly_cost_limit_yen > 0:
        exceeded, _estimate, current = will_exceed_limit(
            db,
            parsed_duration,
            model_tier,
            settings.monthly_cost_limit_yen,
        )
        if exceeded:
            reset_text = next_month_start_jst_text()
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "MONTHLY_BUDGET_EXCEEDED",
                    "message": (
                        f"今月の文字起こし予算 (¥{settings.monthly_cost_limit_yen:,}) "
                        f"に達したため、新規のアップロードを停止しています。"
                        f"次の月初 ({reset_text}) にリセットされます。"
                        f"早めにご利用が必要な場合は管理者にご相談ください。"
                        f"（今月の利用額: ¥{current:,}）"
                    ),
                },
            )

    # ──── ストレージ容量チェック (v1.0.2) ──────────────────────────────────
    # Content-Length が分かれば受信開始前に拒否できる。これでアップロード
    # 時間を浪費せずに済む。クライアント側でも予防警告するが、最終的な
    # 安全網はここ。
    incoming_size = file.size if file.size is not None else 0
    if incoming_size > 0 and would_exceed_hard_limit(incoming_size):
        usage = get_storage_summary()
        raise HTTPException(
            status_code=413,
            detail={
                "code": "STORAGE_HARD_LIMIT_EXCEEDED",
                "message": (
                    f"ストレージが満杯です ({usage.used_pretty} / {usage.limit_pretty})。"
                    f"古い文字起こしを削除してから再度アップロードしてください。"
                ),
                "usage": {
                    "used_bytes": usage.used_bytes,
                    "limit_bytes": usage.limit_bytes,
                    "percent": usage.percent,
                },
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
    except Exception as exc:
        logger.exception("ファイル一時保存に失敗: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "FILE_SAVE_ERROR",
                "message": f"ファイル保存に失敗しました ({type(exc).__name__}): {exc}",
            },
        )

    # 受信完了後にもう一度ストレージ容量を確認する。受信中に他ユーザーの
    # アップロードが完了して上限を超えるレース状態を吸収する。超過時は
    # 保存した tmp を掃除して 413 を返す。
    if would_exceed_hard_limit(size_bytes):
        try:
            save_path.unlink(missing_ok=True)
            cleanup_dir = save_path.parent
            if cleanup_dir.exists():
                import shutil
                shutil.rmtree(cleanup_dir, ignore_errors=True)
        except Exception:
            logger.exception("容量超過の保存ファイル掃除失敗")
        usage = get_storage_summary()
        raise HTTPException(
            status_code=413,
            detail={
                "code": "STORAGE_HARD_LIMIT_EXCEEDED",
                "message": (
                    f"ストレージが満杯です ({usage.used_pretty} / {usage.limit_pretty})。"
                    f"古い文字起こしを削除してから再度アップロードしてください。"
                ),
                "usage": {
                    "used_bytes": usage.used_bytes,
                    "limit_bytes": usage.limit_bytes,
                    "percent": usage.percent,
                },
            },
        )

    # ──── DB レコード作成 ──────────────────────────────────────────────────
    # AssemblyAI 未設定の時は uploaded（処理されない）、設定済なら processing
    initial_status = "processing" if settings.has_assemblyai else "uploaded"

    try:
        from datetime import datetime as _dt
        parsed_meeting_date: datetime | None = None
        if meeting_date and meeting_date.strip():
            try:
                parsed_meeting_date = _dt.fromisoformat(meeting_date.strip())
            except ValueError:
                pass

        transcript = Transcript(
            user_id=user["id"],
            original_filename=file.filename,
            file_size_bytes=size_bytes,
            audio_duration_seconds=parsed_duration,
            status=initial_status,
            model_tier=model_tier,
            language="ja",
            created_at=datetime.now(timezone.utc),
            project_id=parsed_project_id,
            meeting_date=parsed_meeting_date,
            meeting_location=meeting_location.strip() if meeting_location else None,
            meeting_purpose=meeting_purpose.strip() if meeting_purpose else None,
            meeting_agenda=meeting_agenda.strip() if meeting_agenda else None,
        )
        db.add(transcript)
        db.commit()
        db.refresh(transcript)
    except Exception as exc:
        logger.exception("DB レコード作成に失敗: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DB_ERROR",
                "message": f"データベースエラーが発生しました ({type(exc).__name__}): {exc}",
            },
        )

    logger.info(
        "Transcript レコード作成: id=%s user_id=%s filename=%s size=%s tier=%s status=%s",
        transcript.id,
        user["id"],
        file.filename,
        size_bytes,
        model_tier,
        initial_status,
    )

    # ──── word_boost と custom_spelling を自動収集 ────────────────────────
    # ユーザー入力語 → PJT固有名詞辞書 → People台帳の名前 の順でマージし上限50語
    auto_boost: list[str] = []
    auto_custom_spelling: list[dict] = []
    vocab_rows: list = []
    if parsed_project_id:
        vocab_rows = list(db.scalars(
            select(ProjectVocabulary).where(ProjectVocabulary.project_id == parsed_project_id)
        ))
        for v in vocab_rows:
            if v.word:
                auto_boost.append(v.word)
            if v.reading and v.word:
                auto_custom_spelling.append({"from": [v.reading], "to": v.word})
    people_names = list(db.scalars(
        select(Person.name).where(Person.owner_user_id == user["id"])
    ))
    auto_boost.extend(people_names)

    merged = list(dict.fromkeys(user_word_boost + auto_boost))  # 重複排除・順序保持
    parsed_word_boost = merged[:50] if merged else None
    parsed_custom_spelling = auto_custom_spelling if auto_custom_spelling else None

    logger.info(
        "word_boost 自動補完: user=%s pjt_vocab=%s people=%s total=%s custom_spelling=%s",
        len(user_word_boost), len(vocab_rows), len(people_names),
        len(merged), len(auto_custom_spelling),
    )

    # ──── バックグラウンドで文字起こしを開始 ───────────────────────────
    if settings.has_assemblyai:
        # asyncio.create_task で fire-and-forget。
        # レスポンスが返った後も event loop 上で動き続ける。
        asyncio.create_task(process_transcript(transcript.id, save_path, speakers_expected=parsed_speakers, word_boost=parsed_word_boost, custom_spelling=parsed_custom_spelling))
    else:
        logger.warning(
            "ASSEMBLYAI_API_KEY 未設定のため、ジョブ %s は uploaded 状態のままです",
            transcript.id,
        )

    # ──── レスポンス（HTMX で履歴行を append する HTML 断片） ───────────
    try:
        return templates.TemplateResponse(
            request,
            "_transcript_row.html",
            {"transcript": transcript},
        )
    except Exception as exc:
        logger.exception("テンプレート描画に失敗: transcript_id=%s %s", transcript.id, exc)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "TEMPLATE_ERROR",
                "message": f"レスポンス生成に失敗しました ({type(exc).__name__}): {exc}",
            },
        )


@router.get("/api/transcripts", response_class=HTMLResponse)
async def list_transcripts(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """履歴一覧を返す（HTMX で部分更新するための HTML）。"""
    my_stmt = (
        select(Transcript)
        .where(Transcript.user_id == user["id"])
        .where(Transcript.deleted_at.is_(None))
        .order_by(Transcript.created_at.desc())
        .limit(100)
    )
    transcripts = list(db.scalars(my_stmt))

    # 共有されたファイル（自分がオーナーでないもの）
    shared_ids = db.scalars(
        select(TranscriptShare.transcript_id)
        .where(TranscriptShare.shared_with_user_id == user["id"])
    ).all()
    shared_transcripts: list[Transcript] = []
    if shared_ids:
        shared_transcripts = list(db.scalars(
            select(Transcript)
            .where(Transcript.id.in_(shared_ids))
            .where(Transcript.deleted_at.is_(None))
            .order_by(Transcript.created_at.desc())
        ))

    return templates.TemplateResponse(
        request,
        "_transcript_list.html",
        {"transcripts": transcripts, "shared_transcripts": shared_transcripts},
    )


@router.patch("/api/transcripts/{transcript_id}")
async def update_transcript(
    transcript_id: int,
    payload: dict,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """文字起こしの表示名 (title) を更新する。

    request body: {"title": "山田さんとの 1on1"} もしくは {"title": ""}（クリア）
    title は最大 500 文字。空文字を渡すと NULL に戻し original_filename 表示に
    戻す。
    """
    transcript = db.get(Transcript, transcript_id)
    if (
        transcript is None
        or transcript.user_id != user["id"]
        or transcript.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    raw = payload.get("title")
    if raw is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_TITLE", "message": "title フィールドが必要です"},
        )
    title = str(raw).strip()
    if len(title) > 500:
        raise HTTPException(
            status_code=400,
            detail={"code": "TITLE_TOO_LONG", "message": "名前は 500 文字以内で入力してください"},
        )
    transcript.title = title or None
    db.commit()
    db.refresh(transcript)
    return JSONResponse(
        {
            "id": transcript.id,
            "title": transcript.title,
            "original_filename": transcript.original_filename,
        }
    )


@router.get("/api/transcripts/{transcript_id}/audio")
async def get_audio(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> FileResponse:
    """音声ファイルを配信する（HTML5 <audio> 要素から参照される）。

    認証必須。所有権チェックを行う。ファイルが存在しない場合 404。
    Range リクエストは FileResponse が自動で扱う（シーク・部分再生に対応）。
    """
    transcript = db.get(Transcript, transcript_id)
    if (
        transcript is None
        or not _can_access(transcript, user["id"], db)
        or transcript.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    path = find_stored_audio(transcript_id)
    if path is None or not path.exists():
        raise HTTPException(
            status_code=404,
            detail={"code": "AUDIO_NOT_FOUND", "message": "音声ファイルが見つかりません"},
        )

    suffix = path.suffix.lower()
    media_type = MEDIA_TYPES.get(suffix, "application/octet-stream")
    return FileResponse(path=str(path), media_type=media_type)


@router.delete("/api/transcripts/{transcript_id}/audio")
async def delete_audio(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """音声ファイルだけを削除する（文字起こしテキストは残す）。"""
    transcript = db.get(Transcript, transcript_id)
    if (
        transcript is None
        or transcript.user_id != user["id"]
        or transcript.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    deleted = delete_stored_audio(transcript_id)
    return JSONResponse({"deleted": deleted})


@router.delete("/api/transcripts/{transcript_id}", status_code=200)
async def delete_transcript(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """文字起こしをソフト削除する（deleted_at を設定）。

    HTMX 側は hx-swap="delete" でこの行を DOM から削除する。
    """
    transcript = db.get(Transcript, transcript_id)
    if (
        transcript is None
        or transcript.user_id != user["id"]
        or transcript.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    transcript.deleted_at = datetime.now(timezone.utc)
    db.commit()
    # 永続側の音声も削除する (ソフト削除でテキストは残るが、音声は復活する意味が薄い)
    delete_stored_audio(transcript_id)
    # HTMX が delete swap を実行するために 2xx を返す（ボディ不要）
    return Response(status_code=200, content="")


class ProjectAssignBody(BaseModel):
    project_id: int | None = None


@router.patch("/api/transcripts/{transcript_id}/project")
async def update_transcript_project(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: ProjectAssignBody,
) -> JSONResponse:
    """文字起こしのプロジェクト帰属を変更する（あとから追加・移動・解除）。"""
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or transcript.user_id != user["id"] or transcript.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
    transcript.project_id = body.project_id
    db.commit()
    return JSONResponse({"ok": True, "project_id": body.project_id})


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


@router.get("/transcripts/{transcript_id}", response_class=HTMLResponse)
async def transcript_detail(
    request: Request,
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """文字起こし詳細ページ（カード形式の発言表示・編集 UI）。"""
    settings = load_settings()
    is_admin = user["email"].lower() == settings.admin_email
    transcript = db.get(Transcript, transcript_id)
    if (
        transcript is None
        or transcript.deleted_at is not None
        or (not is_admin and not _can_access(transcript, user["id"], db))
    ):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
    is_owner = _is_owner(transcript, user["id"]) or is_admin

    segments = list(
        db.scalars(
            select(Segment)
            .where(Segment.transcript_id == transcript_id)
            .order_by(Segment.order_index)
        )
    )
    speakers = list(
        db.scalars(
            select(Speaker).where(Speaker.transcript_id == transcript_id)
        )
    )
    # speaker_label → display_name の辞書
    speaker_name_map = {s.speaker_label: s.display_name for s in speakers}

    # 同じ表示名（effective name）に同じ色を割り当てるためのマップ。
    # 出現順に色 0〜7 を循環。
    name_to_color: dict[str, int] = {}
    for seg in segments:
        effective = (
            seg.display_name
            or speaker_name_map.get(seg.speaker_label)
            or seg.speaker_label
        )
        if effective not in name_to_color:
            name_to_color[effective] = len(name_to_color) % 8

    # ユーザーが過去に使った話者名（候補リスト）
    history_names = _user_speaker_history_names(user["id"], db)
    people_registry = _user_people_registry(user["id"], db)

    # 参加者リスト（JSON テキスト → Python list）
    import json as _json
    try:
        meeting_participants: list[str] = _json.loads(transcript.meeting_participants) if transcript.meeting_participants else []
    except (ValueError, TypeError):
        meeting_participants = []

    return templates.TemplateResponse(
        request,
        "transcript_detail.html",
        {
            "app_version": APP_VERSION,
            "env": settings.env,
            "user": user,
            "is_admin": user["email"].lower() == settings.admin_email,
            "transcript": transcript,
            "segments": segments,
            "speakers": speakers,
            "speaker_name_map": speaker_name_map,
            "name_to_color": name_to_color,
            "history_names": history_names,
            "people_registry": people_registry,
            "meeting_participants": meeting_participants,
            "is_owner": is_owner,
        },
    )


def _user_people_registry(user_id: int, db: Session) -> dict:
    """People台帳を {name: {id, name, company, job_title, role}} で返す。"""
    rows = db.scalars(
        select(Person).where(Person.owner_user_id == user_id)
    )
    return {
        p.name: {
            "id": p.id,
            "name": p.name,
            "company": p.company or "",
            "job_title": p.job_title or "",
            "role": p.role or "",
        }
        for p in rows
    }


def _user_speaker_history_names(user_id: int, db: Session) -> list[str]:
    """話者リネームの候補 = 自分の名前 + People台帳。
    speaker_history（過去の使用履歴）はゴミが混入するため除外。"""
    db_user = db.get(User, user_id)
    own_name = db_user.name if db_user else None

    people_names = list(db.scalars(
        select(Person.name)
        .where(Person.owner_user_id == user_id)
        .order_by(Person.name)
    ))

    # 自分の名前を先頭に（People台帳に同名がなければ追加）
    if own_name and own_name not in people_names:
        return [own_name] + people_names
    return people_names


def _link_speaker_to_person(speaker: Speaker, to_name: str, user_id: int, db: Session) -> None:
    """話者リネーム時に Person レコードへリンクする。
    People台帳に同名がなければ自動作成する（話者リネームだけで台帳が育つ）。"""
    from datetime import datetime, timezone as _tz
    person = db.scalars(
        select(Person).where(Person.owner_user_id == user_id, Person.name == to_name)
    ).first()
    if person is None:
        person = Person(
            owner_user_id=user_id,
            name=to_name,
            created_at=datetime.now(_tz.utc),
        )
        db.add(person)
        db.flush()
    speaker.person_id = person.id


@router.get("/api/transcripts/{transcript_id}/status")
async def get_transcript_status(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """軽量なステータス確認用 JSON エンドポイント。"""
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or not _can_access(transcript, user["id"], db):
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


# ── セグメント編集 ─────────────────────────────────────────────────────


def _record_speaker_history(user_id: int, name: str, db: Session) -> None:
    """話者名の使用履歴を記録する。重複しない名前のみ。

    同一ユーザーが複数タブで同じ名前をほぼ同時にリネームしても、
    UniqueConstraint(user_id, name) 違反で 500 にならないよう
    SQLite の UPSERT (INSERT ... ON CONFLICT DO UPDATE) で原子的に処理する。
    """
    name = (name or "").strip()
    if not name:
        return

    now = datetime.now(timezone.utc)
    stmt = sqlite_insert(SpeakerHistory).values(
        user_id=user_id,
        name=name,
        last_used_at=now,
        use_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "name"],
        set_={
            "last_used_at": now,
            "use_count": SpeakerHistory.__table__.c.use_count + 1,
        },
    )
    db.execute(stmt)


def _check_speaker_name_collision(
    transcript_id: int,
    new_name: str,
    affected_segment_ids: list[int],
    db: Session,
) -> str | None:
    """同じ話者名 (effective_name) が複数の異なる speaker_label に重ならないか検証する。

    Notta 等と同じ「ユーザー側で工夫する (例: 田中A、田中B)」方針。

    Returns:
        OK なら None、衝突するならエラーメッセージ。
    """
    target = (new_name or "").strip()
    if not target:
        return None

    segments = list(
        db.scalars(select(Segment).where(Segment.transcript_id == transcript_id))
    )
    speakers = list(
        db.scalars(select(Speaker).where(Speaker.transcript_id == transcript_id))
    )
    name_map = {s.speaker_label: s.display_name for s in speakers}
    affected_set = set(affected_segment_ids)

    labels_with_target: set[str] = set()

    # 1. これから書き換えるセグメントは target になる → speaker_label を集める
    for seg in segments:
        if seg.id in affected_set:
            labels_with_target.add(seg.speaker_label)

    # 2. 書き換えないセグメントで、現状すでに target を表示しているもの
    for seg in segments:
        if seg.id in affected_set:
            continue
        effective = (
            seg.display_name or name_map.get(seg.speaker_label) or seg.speaker_label
        )
        if (effective or "").strip() == target:
            labels_with_target.add(seg.speaker_label)

    if len(labels_with_target) > 1:
        return (
            f"「{target}」は別の話者で既に使われています。"
            f"別の名前にしてください（例: {target}A、{target}B）。"
        )
    return None


@router.patch("/api/segments/{segment_id}")
async def update_segment(
    segment_id: int,
    payload: dict,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """セグメント1件を更新（テキスト or 個別話者上書き）。"""
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    transcript = db.get(Transcript, segment.transcript_id)
    if transcript is None or not _can_access(transcript, user["id"], db):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    # text 更新
    if "text" in payload:
        new_text = (payload.get("text") or "").strip()
        if not new_text:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_TEXT", "message": "テキストは空にできません"},
            )
        segment.text_content = new_text
        segment.is_edited = True

    # display_name 個別上書き（カード単位の話者変更）
    if "display_name" in payload:
        name = payload.get("display_name")
        force = bool(payload.get("force"))
        if name is None or name == "":
            segment.display_name = None
        else:
            name = str(name).strip()
            if name and not force:
                # 同名禁止チェック: 別の speaker_label に同じ表示名が
                # 付いていたら拒否する。force=True (候補からの選択 = 既存
                # 話者への明示的マージ) のときはスキップする。
                collision_msg = _check_speaker_name_collision(
                    transcript.id, name, [segment.id], db
                )
                if collision_msg is not None:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "DUPLICATE_SPEAKER_NAME",
                            "message": collision_msg,
                        },
                    )
            segment.display_name = name or None
            if name:
                _record_speaker_history(user["id"], name, db)

    db.commit()
    db.refresh(segment)

    return JSONResponse(
        {
            "id": segment.id,
            "text": segment.text_content,
            "display_name": segment.display_name,
            "is_edited": segment.is_edited,
        }
    )


# ── セグメント保存確認（自動保存の自己検証用） ─────────────────────


@router.get("/api/transcripts/{transcript_id}/segments/count")
async def get_segments_count(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """保存済みセグメント件数を返す。フロントエンドの自動保存検証用。"""
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or not _can_access(transcript, user["id"], db) or transcript.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    from sqlalchemy import func
    from backend.db.models import Segment
    count = db.scalar(
        select(func.count()).where(Segment.transcript_id == transcript_id)
    )
    return JSONResponse({"count": count, "transcript_id": transcript_id})


# ── セグメント分割（Shift+Return で1つを2つに分ける） ──────────────


@router.post("/api/segments/{segment_id}/split")
async def split_segment(
    segment_id: int,
    payload: dict,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """セグメントを文字位置 ``position`` で2つに分割する。

    request body: {"position": int}  (現在のテキスト内のカーソル位置)

    挙動:
    - text[:position] を現セグメントに残す
    - text[position:] を新セグメントとして直後に挿入
    - 新セグメントは話者ラベルを継承
    - 後続セグメントの order_index は +1 ずらす
    - 時間は現セグメントを按分する（簡易）

    レスポンス:
    {
        "current": {"id": ..., "text": ...},
        "new":     {"id": ..., "order_index": ..., "start_seconds": ..., ...}
    }
    """
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    transcript = db.get(Transcript, segment.transcript_id)
    if transcript is None or not _can_access(transcript, user["id"], db):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    raw_position = payload.get("position")
    try:
        position = int(raw_position)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_POSITION", "message": "位置の指定が不正です"},
        )

    full_text = segment.text_content or ""
    position = max(0, min(position, len(full_text)))

    before = full_text[:position].rstrip()
    after = full_text[position:].lstrip()

    if not before and not after:
        raise HTTPException(
            status_code=400,
            detail={"code": "EMPTY_SPLIT", "message": "空のセグメントは分割できません"},
        )

    # 時間の按分（文字数比率）。元のテキスト長が 0 のときは半分にする。
    original_end = segment.end_seconds
    duration = original_end - segment.start_seconds
    if len(full_text) > 0 and position > 0:
        ratio = position / len(full_text)
    else:
        ratio = 0.5
    split_time = segment.start_seconds + duration * ratio

    # 後続セグメントの order_index を +1 ずらす（重複しないよう新セグメントの席を作る）
    db.execute(
        Segment.__table__.update()
        .where(Segment.transcript_id == segment.transcript_id)
        .where(Segment.order_index > segment.order_index)
        .values(order_index=Segment.order_index + 1)
    )

    # 現セグメントを更新（前半テキスト）
    segment.text_content = before or "（無音）"
    segment.end_seconds = split_time
    segment.is_edited = True

    # 新セグメントを追加（後半テキスト）
    new_segment = Segment(
        transcript_id=segment.transcript_id,
        order_index=segment.order_index + 1,
        start_seconds=split_time,
        end_seconds=original_end,
        speaker_label=segment.speaker_label,
        text_content=after or "（無音）",
        is_edited=True,
    )

    db.add(new_segment)
    db.commit()
    db.refresh(segment)
    db.refresh(new_segment)

    return JSONResponse(
        {
            "current": {
                "id": segment.id,
                "text": segment.text_content,
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "is_edited": segment.is_edited,
            },
            "new": {
                "id": new_segment.id,
                "order_index": new_segment.order_index,
                "start_seconds": new_segment.start_seconds,
                "end_seconds": new_segment.end_seconds,
                "speaker_label": new_segment.speaker_label,
                "text": new_segment.text_content,
                "is_edited": new_segment.is_edited,
            },
        }
    )


# ── 話者一括リネーム ───────────────────────────────────────────────────


# ── エクスポート ───────────────────────────────────────────────────────


def _format_time_hms(seconds: float) -> str:
    """秒数を HH:MM:SS 形式に整形（時間が 0 なら MM:SS）。"""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_time_srt(seconds: float) -> str:
    """SRT 形式のタイムコード（HH:MM:SS,mmm）。"""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    ms = int((seconds - total) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _resolve_speaker_name(seg: Segment, name_map: dict[str, str]) -> str:
    """セグメントの表示名を解決する。segment.display_name 優先、なければ speakers から。"""
    if seg.display_name:
        return seg.display_name
    return name_map.get(seg.speaker_label, seg.speaker_label)


def _build_export_txt(segments: list[Segment], name_map: dict[str, str]) -> str:
    lines: list[str] = []
    for seg in segments:
        name = _resolve_speaker_name(seg, name_map)
        ts = _format_time_hms(seg.start_seconds)
        lines.append(f"[{ts} {name}]")
        lines.append(seg.text_content)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_export_srt(segments: list[Segment], name_map: dict[str, str]) -> str:
    parts: list[str] = []
    for i, seg in enumerate(segments, 1):
        name = _resolve_speaker_name(seg, name_map)
        parts.append(str(i))
        parts.append(
            f"{_format_time_srt(seg.start_seconds)} --> {_format_time_srt(seg.end_seconds)}"
        )
        parts.append(f"[{name}] {seg.text_content}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _build_export_json(
    transcript: Transcript, segments: list[Segment], name_map: dict[str, str]
) -> str:
    import json

    data = {
        "version": 1,
        "transcript": {
            "id": transcript.id,
            "filename": transcript.original_filename,
            "audio_duration_seconds": transcript.audio_duration_seconds,
            "created_at": (
                transcript.created_at.isoformat() if transcript.created_at else None
            ),
            "completed_at": (
                transcript.completed_at.isoformat()
                if transcript.completed_at
                else None
            ),
        },
        "speakers": [
            {"speaker_label": label, "display_name": name}
            for label, name in name_map.items()
        ],
        "segments": [
            {
                "order_index": s.order_index,
                "start_seconds": s.start_seconds,
                "end_seconds": s.end_seconds,
                "speaker_label": s.speaker_label,
                "display_name": s.display_name,
                "text": s.text_content,
                "speaker": _resolve_speaker_name(s, name_map),
            }
            for s in segments
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


@router.get("/api/transcripts/{transcript_id}/export")
async def export_transcript(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    format: str = "txt",
) -> Response:
    """文字起こしを TXT / SRT / JSON でダウンロード。"""
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or not _can_access(transcript, user["id"], db) or transcript.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    segments = list(
        db.scalars(
            select(Segment)
            .where(Segment.transcript_id == transcript_id)
            .order_by(Segment.order_index)
        )
    )
    speakers = list(
        db.scalars(
            select(Speaker).where(Speaker.transcript_id == transcript_id)
        )
    )
    name_map = {s.speaker_label: s.display_name for s in speakers}

    fmt = format.lower()
    if fmt == "txt":
        content = _build_export_txt(segments, name_map)
        media_type = "text/plain; charset=utf-8"
        ext = "txt"
    elif fmt == "srt":
        content = _build_export_srt(segments, name_map)
        media_type = "application/x-subrip; charset=utf-8"
        ext = "srt"
    elif fmt == "json":
        content = _build_export_json(transcript, segments, name_map)
        media_type = "application/json; charset=utf-8"
        ext = "json"
    else:
        raise HTTPException(
            status_code=400,
            detail={"code": "UNKNOWN_FORMAT", "message": "未対応の形式です"},
        )

    # ファイル名: リネームがあれば title、なければ original_filename（拡張子除く）
    import os

    raw_name = transcript_display_name(transcript)
    base = os.path.splitext(raw_name)[0] or "transcript"
    download_name = f"{base}.{ext}"

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                # RFC 5987 形式で日本語ファイル名を扱う
                f"attachment; filename*=UTF-8''{_urlencode_filename(download_name)}"
            )
        },
    )


def _urlencode_filename(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")


@router.post("/api/transcripts/{transcript_id}/segments/rename-by-name")
async def rename_segments_by_effective_name(
    transcript_id: int,
    payload: dict,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """画面上の表示名（effective name）が一致するセグメントをまとめて改名する。

    Notta 方式の「すべての○○ に適用」用エンドポイント。
    元の speaker_label は無視し、現在表示されている名前が ``from_name`` の
    セグメントすべてに対し ``segment.display_name = to_name`` を個別にセットする。

    payload: {"from_name": "話者A", "to_name": "山田さん"}
    """
    transcript = db.get(Transcript, transcript_id)
    if transcript is None or not _can_access(transcript, user["id"], db):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    from_name = (payload.get("from_name") or "").strip()
    to_name = (payload.get("to_name") or "").strip()
    force = bool(payload.get("force"))
    if not from_name or not to_name:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_NAME", "message": "名前は空にできません"},
        )

    segments = list(
        db.scalars(select(Segment).where(Segment.transcript_id == transcript_id))
    )
    speakers = list(
        db.scalars(select(Speaker).where(Speaker.transcript_id == transcript_id))
    )
    name_map = {s.speaker_label: s.display_name for s in speakers}

    # 影響を受けるセグメントを先に確定し、同名禁止チェック
    matched_ids: list[int] = []
    for seg in segments:
        effective = seg.display_name or name_map.get(seg.speaker_label) or seg.speaker_label
        if effective == from_name:
            matched_ids.append(seg.id)

    # force=True は「候補からの選択 = 既存話者への明示的マージ」を表すので
    # 重複チェックをスキップする。
    if not force:
        collision_msg = _check_speaker_name_collision(
            transcript_id, to_name, matched_ids, db
        )
        if collision_msg is not None:
            raise HTTPException(
                status_code=400,
                detail={"code": "DUPLICATE_SPEAKER_NAME", "message": collision_msg},
            )

    # 検証通過したので実際に書き換える
    matched_labels: set[str] = set()
    for seg in segments:
        if seg.id in matched_ids:
            seg.display_name = to_name
            matched_labels.add(seg.speaker_label)

    # リネームに関与した Speaker レコードを People台帳にリンク
    speaker_map = {s.speaker_label: s for s in speakers}
    for label in matched_labels:
        if label in speaker_map:
            _link_speaker_to_person(speaker_map[label], to_name, user["id"], db)

    _record_speaker_history(user["id"], to_name, db)
    db.commit()

    return JSONResponse(
        {
            "from_name": from_name,
            "to_name": to_name,
            "segment_ids": matched_ids,
        }
    )
