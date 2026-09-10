"""プロジェクト管理（大テーマ → プロジェクト）のHTTPルート。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.auth.dependencies import CurrentUser
from backend.config import APP_VERSION, load_settings
from backend.db import get_db
from backend.db.models import Attachment, Person, PolishLog, Project, ProjectMember, ProjectVocabulary, Segment, Speaker, Theme, Transcript, User
from datetime import timezone as _tz
from backend.transcribe.cost import get_cost_summary
from backend.transcribe.display import has_stored_audio, transcript_display_name
from backend.transcribe.retention import expiry_status
from backend.transcribe.storage_usage import get_summary as get_storage_summary
from backend.transcribe.polish import (
    MODELS as POLISH_MODELS,
    build_context_text,
    estimate_tokens,
    estimate_cost,
    pickup_proper_nouns,
    run_polish,
)

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.globals["display_name"] = transcript_display_name
templates.env.globals["has_audio"] = has_stored_audio
templates.env.globals["expiry_status"] = expiry_status
templates.env.globals["storage_usage"] = get_storage_summary
templates.env.globals["cost_usage"] = get_cost_summary

router = APIRouter()

settings = load_settings()


# ── Pydantic スキーマ ────────────────────────────────────────────────────────

class ThemeCreate(BaseModel):
    name: str
    description: str | None = None


class ThemeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class MemberAdd(BaseModel):
    user_id: int | None = None
    name: str | None = None
    company: str | None = None
    job_title: str | None = None
    project_role: str | None = None


class MemberUpdate(BaseModel):
    name: str | None = None
    company: str | None = None
    job_title: str | None = None
    project_role: str | None = None


class VocabularyAdd(BaseModel):
    word: str
    meaning: str | None = None
    reading: str | None = None


class VocabularyUpdate(BaseModel):
    word: str | None = None
    meaning: str | None = None
    reading: str | None = None


class TranscriptMetadataUpdate(BaseModel):
    meeting_date: str | None = None       # ISO 8601 文字列、フロントから渡す
    meeting_location: str | None = None
    overview: str | None = None
    meeting_purpose: str | None = None
    meeting_agenda: str | None = None
    meeting_participants: list[str] | None = None  # 参加者名リスト


class ProfileUpdate(BaseModel):
    company: str | None = None
    job_title: str | None = None


class PersonCreate(BaseModel):
    name: str
    company: str | None = None
    job_title: str | None = None
    role: str | None = None


class PersonUpdate(BaseModel):
    name: str | None = None
    company: str | None = None
    job_title: str | None = None
    role: str | None = None


# ── ヘルパー ────────────────────────────────────────────────────────────────

def _get_project_or_404(project_id: int, user_id: int, db: Session) -> Project:
    """プロジェクトを取得する。存在しなければ 404。"""
    proj = db.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
    return proj


def _get_theme_or_404(theme_id: int, db: Session) -> Theme:
    theme = db.get(Theme, theme_id)
    if not theme:
        raise HTTPException(status_code=404, detail="テーマが見つかりません")
    return theme


def _common_ctx(user: dict, db: Session) -> dict:
    """全ページで共通のテンプレートコンテキスト。"""
    return {
        "app_version": APP_VERSION,
        "env": settings.env,
        "user": user,
        "is_admin": user["email"].lower() == settings.admin_email,
    }


def _member_display_name(member: ProjectMember) -> str:
    """メンバーの表示名を返す（社内ユーザーなら DB の name、社外なら入力 name）。"""
    if member.user:
        return member.user.name
    return member.name or "（名前なし）"


# ── テーマ ─────────────────────────────────────────────────────────────────

@router.get("/themes", response_class=HTMLResponse)
async def themes_page(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """テーマ一覧ページ。"""
    themes = list(
        db.scalars(
            select(Theme)
            .options(selectinload(Theme.projects))
            .order_by(Theme.created_at.desc())
        )
    )
    standalone = list(
        db.scalars(
            select(Project)
            .where(Project.theme_id.is_(None))
            .order_by(Project.created_at.desc())
        )
    )
    ctx = _common_ctx(user, db)
    ctx.update({"themes": themes, "standalone_projects": standalone})
    return templates.TemplateResponse(request, "themes.html", ctx)


@router.post("/api/themes", status_code=status.HTTP_201_CREATED)
async def create_theme(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: ThemeCreate,
) -> JSONResponse:
    """テーマを作成する。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="テーマ名は必須です")
    theme = Theme(
        name=name,
        description=body.description,
        created_by_user_id=user["id"],
    )
    db.add(theme)
    db.commit()
    db.refresh(theme)
    return JSONResponse({"id": theme.id, "name": theme.name}, status_code=201)


@router.post("/api/themes/{theme_id}")
async def update_theme(
    theme_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: ThemeUpdate,
) -> JSONResponse:
    """テーマを更新する。"""
    theme = _get_theme_or_404(theme_id, db)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="テーマ名は必須です")
        theme.name = name
    if body.description is not None:
        theme.description = body.description
    db.commit()
    return JSONResponse({"id": theme.id, "name": theme.name})


@router.delete("/api/themes/{theme_id}")
async def delete_theme(
    theme_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """テーマを削除する（配下のプロジェクトも cascade 削除）。"""
    theme = _get_theme_or_404(theme_id, db)
    db.delete(theme)
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/api/themes/{theme_id}/projects", status_code=status.HTTP_201_CREATED)
async def create_project_under_theme(
    theme_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: ProjectCreate,
) -> JSONResponse:
    """テーマ配下にプロジェクトを作成する。"""
    _get_theme_or_404(theme_id, db)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="プロジェクト名は必須です")
    proj = Project(
        theme_id=theme_id,
        name=name,
        description=body.description,
        created_by_user_id=user["id"],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return JSONResponse({"id": proj.id, "name": proj.name}, status_code=201)


# ── プロジェクト ────────────────────────────────────────────────────────────

@router.post("/api/projects", status_code=status.HTTP_201_CREATED)
async def create_standalone_project(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: ProjectCreate,
) -> JSONResponse:
    """スタンドアロンプロジェクト（theme_id=None）を作成する。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="プロジェクト名は必須です")
    proj = Project(
        theme_id=None,
        name=name,
        description=body.description,
        created_by_user_id=user["id"],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return JSONResponse({"id": proj.id, "name": proj.name}, status_code=201)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail_page(
    project_id: int,
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """プロジェクト詳細ページ。"""
    proj = db.scalars(
        select(Project)
        .options(
            selectinload(Project.theme),
            selectinload(Project.members).selectinload(ProjectMember.user),
            selectinload(Project.vocabulary),
        )
        .where(Project.id == project_id)
    ).first()
    if not proj:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")

    # 関連文字起こし
    related_transcripts = list(
        db.scalars(
            select(Transcript)
            .where(Transcript.project_id == project_id)
            .where(Transcript.deleted_at.is_(None))
            .order_by(Transcript.created_at.desc())
        )
    )

    # 社内ユーザー一覧（メンバー追加用）
    all_users = list(db.scalars(select(User).order_by(User.name)))

    # このPJTの音声に登場した人物（Speaker→People リンク済み、重複排除）
    transcript_ids = [t.id for t in related_transcripts]
    project_people: list[Person] = []
    if transcript_ids:
        seen_ids: set[int] = set()
        speakers_with_person = db.scalars(
            select(Speaker)
            .options(selectinload(Speaker.person))
            .where(
                Speaker.transcript_id.in_(transcript_ids),
                Speaker.person_id.isnot(None),
            )
        )
        for s in speakers_with_person:
            if s.person and s.person.id not in seen_ids:
                seen_ids.add(s.person.id)
                project_people.append(s.person)
        project_people.sort(key=lambda p: p.name)

    ctx = _common_ctx(user, db)
    ctx.update({
        "project": proj,
        "related_transcripts": related_transcripts,
        "all_users": all_users,
        "member_display_name": _member_display_name,
        "project_people": project_people,
    })
    return templates.TemplateResponse(request, "project_detail.html", ctx)


@router.post("/api/projects/{project_id}")
async def update_project(
    project_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: ProjectUpdate,
) -> JSONResponse:
    """プロジェクトを更新する。"""
    proj = _get_project_or_404(project_id, user["id"], db)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="プロジェクト名は必須です")
        proj.name = name
    if body.description is not None:
        proj.description = body.description
    db.commit()
    return JSONResponse({"id": proj.id, "name": proj.name})


@router.delete("/api/projects/{project_id}")
async def delete_project(
    project_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """プロジェクトを削除する。"""
    proj = _get_project_or_404(project_id, user["id"], db)
    db.delete(proj)
    db.commit()
    return JSONResponse({"ok": True})


# ── メンバー ────────────────────────────────────────────────────────────────

@router.post("/api/projects/{project_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    project_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: MemberAdd,
) -> JSONResponse:
    """プロジェクトにメンバーを追加する。"""
    _get_project_or_404(project_id, user["id"], db)

    member = ProjectMember(
        project_id=project_id,
        user_id=body.user_id,
        name=body.name,
        company=body.company,
        job_title=body.job_title,
        project_role=body.project_role,
    )
    db.add(member)
    db.commit()
    db.refresh(member)

    # 社内ユーザーの場合は name を DB から引く
    display = _member_display_name(member)
    return JSONResponse({
        "id": member.id,
        "display_name": display,
        "company": member.company,
        "job_title": member.job_title,
        "project_role": member.project_role,
        "is_internal": member.user_id is not None,
    }, status_code=201)


@router.post("/api/projects/{project_id}/members/{member_id}")
async def update_member(
    project_id: int,
    member_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: MemberUpdate,
) -> JSONResponse:
    """メンバー情報を更新する（project_role 等）。"""
    _get_project_or_404(project_id, user["id"], db)
    member = db.get(ProjectMember, member_id)
    if not member or member.project_id != project_id:
        raise HTTPException(status_code=404, detail="メンバーが見つかりません")

    fields = body.model_fields_set
    if 'name' in fields:
        member.name = body.name
    if 'company' in fields:
        member.company = body.company
    if 'job_title' in fields:
        member.job_title = body.job_title
    if 'project_role' in fields:
        member.project_role = body.project_role
    db.commit()
    display = _member_display_name(member)
    return JSONResponse({
        "id": member.id,
        "display_name": display,
        "company": member.company,
        "job_title": member.job_title,
        "project_role": member.project_role,
        "is_internal": member.user_id is not None,
    })


@router.delete("/api/projects/{project_id}/members/{member_id}")
async def delete_member(
    project_id: int,
    member_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """メンバーを削除する。"""
    _get_project_or_404(project_id, user["id"], db)
    member = db.get(ProjectMember, member_id)
    if not member or member.project_id != project_id:
        raise HTTPException(status_code=404, detail="メンバーが見つかりません")
    db.delete(member)
    db.commit()
    return JSONResponse({"ok": True})


# ── 固有名詞 ────────────────────────────────────────────────────────────────

@router.post("/api/projects/{project_id}/vocabulary", status_code=status.HTTP_201_CREATED)
async def add_vocabulary(
    project_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: VocabularyAdd,
) -> JSONResponse:
    """固有名詞を追加する。"""
    _get_project_or_404(project_id, user["id"], db)
    word = body.word.strip()
    if not word:
        raise HTTPException(status_code=400, detail="単語は必須です")
    meaning = body.meaning.strip() if body.meaning else None
    reading = body.reading.strip() if body.reading else None
    vocab = ProjectVocabulary(project_id=project_id, word=word, meaning=meaning, reading=reading)
    db.add(vocab)
    db.commit()
    db.refresh(vocab)
    return JSONResponse({"id": vocab.id, "word": vocab.word, "meaning": vocab.meaning, "reading": vocab.reading}, status_code=201)


@router.delete("/api/projects/{project_id}/vocabulary/{vocab_id}")
async def delete_vocabulary(
    project_id: int,
    vocab_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """固有名詞を削除する。"""
    _get_project_or_404(project_id, user["id"], db)
    vocab = db.get(ProjectVocabulary, vocab_id)
    if not vocab or vocab.project_id != project_id:
        raise HTTPException(status_code=404, detail="単語が見つかりません")
    db.delete(vocab)
    db.commit()
    return JSONResponse({"ok": True})


@router.patch("/api/projects/{project_id}/vocabulary/{vocab_id}")
async def update_vocabulary(
    project_id: int,
    vocab_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: VocabularyUpdate,
) -> JSONResponse:
    """固有名詞を更新する。"""
    _get_project_or_404(project_id, user["id"], db)
    vocab = db.get(ProjectVocabulary, vocab_id)
    if not vocab or vocab.project_id != project_id:
        raise HTTPException(status_code=404, detail="単語が見つかりません")
    fields = body.model_fields_set
    if 'word' in fields:
        word = (body.word or "").strip()
        if not word:
            raise HTTPException(status_code=400, detail="単語は必須です")
        vocab.word = word
    if 'reading' in fields:
        vocab.reading = body.reading.strip() if body.reading else None
    if 'meaning' in fields:
        vocab.meaning = body.meaning.strip() if body.meaning else None
    db.commit()
    db.refresh(vocab)
    return JSONResponse({"id": vocab.id, "word": vocab.word, "meaning": vocab.meaning or "", "reading": vocab.reading or ""})


@router.get("/api/projects/{project_id}/available-transcripts")
async def available_transcripts_for_project(
    project_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """このプロジェクトにまだ追加されていない完了済み文字起こし一覧を返す。"""
    _get_project_or_404(project_id, user["id"], db)
    rows = list(db.scalars(
        select(Transcript)
        .where(
            Transcript.user_id == user["id"],
            Transcript.status == "completed",
            Transcript.deleted_at.is_(None),
            (Transcript.project_id != project_id) | Transcript.project_id.is_(None),
        )
        .order_by(Transcript.created_at.desc())
        .limit(200)
    ))
    return JSONResponse([
        {
            "id": t.id,
            "name": transcript_display_name(t),
            "created_at": t.created_at.strftime("%Y/%m/%d"),
            "duration_seconds": t.audio_duration_seconds,
            "project_id": t.project_id,
        }
        for t in rows
    ])


# ── アップロードフォーム用 API ───────────────────────────────────────────────

@router.get("/api/themes-list")
async def themes_list(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """テーマ一覧（プロジェクト情報込み）を返す。"""
    themes = list(
        db.scalars(
            select(Theme)
            .options(selectinload(Theme.projects))
            .order_by(Theme.created_at.desc())
        )
    )
    result = [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "projects": [{"id": p.id, "name": p.name} for p in t.projects],
        }
        for t in themes
    ]
    return JSONResponse(result)


@router.get("/api/projects-list")
async def projects_list(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """プロジェクト一覧（アップロードフォーム選択 + プロジェクトページ共用）。"""
    count_sq = (
        select(Transcript.project_id, func.count().label("cnt"))
        .where(Transcript.deleted_at.is_(None))
        .group_by(Transcript.project_id)
        .subquery()
    )
    rows = db.execute(
        select(Project, count_sq.c.cnt)
        .outerjoin(count_sq, Project.id == count_sq.c.project_id)
        .order_by(Project.name)
    ).all()
    result = [
        {
            "id": p.id,
            "name": p.name,
            "transcript_count": cnt or 0,
        }
        for p, cnt in rows
    ]
    return JSONResponse(result)


@router.get("/api/projects/{project_id}/context")
async def project_context(
    project_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """アップロード時に使うプロジェクトコンテキストを返す。
    vocabulary はテーマの vocabulary + プロジェクトの vocabulary を合算。
    """
    proj = db.scalars(
        select(Project)
        .options(
            selectinload(Project.theme).selectinload(Theme.projects)
            .selectinload(Project.vocabulary),
            selectinload(Project.members).selectinload(ProjectMember.user),
            selectinload(Project.vocabulary),
        )
        .where(Project.id == project_id)
    ).first()
    if not proj:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")

    # 固有名詞: プロジェクト分 + テーマ配下の全プロジェクトからではなく、
    # 「テーマ自体の vocabulary」は Theme モデルには無いので
    # プロジェクトの語彙のみを使う（仕様上テーマレベルの語彙は ProjectVocabulary で管理）
    vocab_words = [{"word": v.word, "meaning": v.meaning} for v in proj.vocabulary]

    members_out = []
    for m in proj.members:
        members_out.append({
            "display_name": _member_display_name(m),
            "project_role": m.project_role or "",
        })

    return JSONResponse({
        "members": members_out,
        "vocabulary": vocab_words,
        "member_count": len(members_out),
    })


# ── 文字起こし OKF メタ情報 ──────────────────────────────────────────────────

@router.post("/api/transcripts/{transcript_id}/metadata")
async def update_transcript_metadata(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: TranscriptMetadataUpdate,
) -> JSONResponse:
    """MTG の OKF メタ情報（日時・場所・目的・アジェンダ）を更新する。"""
    transcript = db.get(Transcript, transcript_id)
    if not transcript or transcript.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="文字起こしが見つかりません")

    if body.meeting_date is not None:
        if body.meeting_date == "":
            transcript.meeting_date = None
        else:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(body.meeting_date)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                transcript.meeting_date = dt
            except ValueError:
                raise HTTPException(status_code=400, detail="meeting_date の形式が不正です")

    if body.meeting_location is not None:
        transcript.meeting_location = body.meeting_location.strip() or None
    if body.overview is not None:
        transcript.overview = body.overview.strip() or None
    if body.meeting_purpose is not None:
        transcript.meeting_purpose = body.meeting_purpose.strip() or None
    if body.meeting_agenda is not None:
        transcript.meeting_agenda = body.meeting_agenda.strip() or None
    if body.meeting_participants is not None:
        import json as _json
        cleaned = [n.strip() for n in body.meeting_participants if n.strip()]
        transcript.meeting_participants = _json.dumps(cleaned, ensure_ascii=False) if cleaned else None

    db.commit()
    return JSONResponse({"ok": True})


# ── 整文（Claude API） ────────────────────────────────────────────────────────

class PolishEstimateRequest(BaseModel):
    model_key: str = "haiku"


class PolishPickupRequest(BaseModel):
    pass


class PolishRunRequest(BaseModel):
    model_key: str = "haiku"
    extra_vocabulary: list[dict] | None = None  # ピックアップ後に追加登録した語句


class PolishAcceptRequest(BaseModel):
    """セグメントごとの採否を受け取る。accepted_texts は {segment_id: text}。"""
    accepted_texts: dict[int, str]


def _get_transcript_or_404(transcript_id: int, user_id: int, db: Session) -> Transcript:
    t = db.get(Transcript, transcript_id)
    if not t or t.user_id != user_id:
        raise HTTPException(status_code=404, detail="文字起こしが見つかりません")
    return t


def _build_transcript_context(transcript: Transcript, db: Session) -> str:
    """トランスクリプトに紐づくPJT/MTGのコンテキストテキストを構築する。"""
    from sqlalchemy.orm import selectinload as _sil
    project = None
    members: list[dict] = []
    vocabulary: list[dict] = []

    if transcript.project_id:
        project = db.scalars(
            select(Project)
            .options(
                selectinload(Project.members).selectinload(ProjectMember.user),
                selectinload(Project.vocabulary),
            )
            .where(Project.id == transcript.project_id)
        ).first()
        if project:
            for m in project.members:
                display = m.user.name if m.user else (m.name or "")
                members.append({
                    "display_name": display,
                    "company": m.company or "",
                    "project_role": m.project_role or "",
                })
            for v in project.vocabulary:
                vocabulary.append({"word": v.word, "meaning": v.meaning or ""})

    # PJTがない場合、Speaker→Peopleリンクから話者情報を補完する
    if not members:
        speakers = list(db.scalars(
            select(Speaker)
            .options(_sil(Speaker.person))
            .where(Speaker.transcript_id == transcript.id, Speaker.person_id.isnot(None))
        ))
        for s in speakers:
            p = s.person
            if p is None:
                continue
            members.append({
                "display_name": s.display_name or p.name,
                "company": p.company or "",
                "project_role": " / ".join(filter(None, [p.job_title, p.role])),
            })

    # meeting_participants を members に補完する（PJTメンバー・話者と重複する名前は除外）
    if transcript.meeting_participants:
        try:
            import json as _json2
            participant_names = _json2.loads(transcript.meeting_participants)
            existing_names = {m["display_name"].lower() for m in members}
            for name in participant_names:
                if name and name.strip() and name.lower() not in existing_names:
                    members.append({"display_name": name.strip(), "company": "", "project_role": ""})
                    existing_names.add(name.lower())
        except Exception:
            pass

    meeting_date_str = None
    if transcript.meeting_date:
        meeting_date_str = transcript.meeting_date.strftime("%Y年%m月%d日 %H:%M")

    # PJT添付資料のサマリーを収集
    project_attachment_summaries: list[str] | None = None
    if project:
        pjt_attachments = list(db.scalars(
            select(Attachment)
            .where(Attachment.project_id == project.id, Attachment.processed_summary.isnot(None))
        ))
        if pjt_attachments:
            project_attachment_summaries = [
                f"[{a.title}]\n{a.processed_summary}" for a in pjt_attachments
            ]

    # MTG添付資料のサマリーを収集
    mtg_attachment_summaries: list[str] | None = None
    mtg_attachments = list(db.scalars(
        select(Attachment)
        .where(Attachment.transcript_id == transcript.id, Attachment.processed_summary.isnot(None))
    ))
    if mtg_attachments:
        mtg_attachment_summaries = [
            f"[{a.title}]\n{a.processed_summary}" for a in mtg_attachments
        ]

    return build_context_text(
        project_name=project.name if project else None,
        project_description=project.description if project else None,
        members=members or None,
        vocabulary=vocabulary or None,
        project_attachments_summaries=project_attachment_summaries,
        meeting_date=meeting_date_str,
        meeting_location=transcript.meeting_location,
        meeting_overview=transcript.overview,
        meeting_purpose=transcript.meeting_purpose,
        meeting_agenda=transcript.meeting_agenda,
        mtg_attachments_summaries=mtg_attachment_summaries,
    )


@router.get("/api/transcripts/{transcript_id}/polish/estimate")
async def polish_estimate(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    model_key: str = "haiku",
) -> JSONResponse:
    """整文の予想コスト・コンテキスト充実度を返す。"""
    transcript = _get_transcript_or_404(transcript_id, user["id"], db)

    # セグメントテキストを結合してトークン数を概算
    segments = list(db.scalars(
        select(Segment)
        .where(Segment.transcript_id == transcript_id)
        .order_by(Segment.order_index)
    ))
    full_text = "\n".join(s.text_content for s in segments)

    context_text = _build_transcript_context(transcript, db)
    input_tokens = estimate_tokens(context_text) + estimate_tokens(full_text) + 500
    output_tokens = estimate_tokens(full_text)

    costs = {k: estimate_cost(input_tokens, output_tokens, k) for k in POLISH_MODELS}

    # コンテキスト充実度（5項目）
    has_project = transcript.project_id is not None
    has_purpose = bool(transcript.meeting_purpose)
    has_agenda = bool(transcript.meeting_agenda)

    vocab_count = 0
    member_count = 0
    if transcript.project_id:
        from sqlalchemy import func
        vocab_count = db.scalar(
            select(func.count()).where(ProjectVocabulary.project_id == transcript.project_id)
        ) or 0
        member_count = db.scalar(
            select(func.count()).where(ProjectMember.project_id == transcript.project_id)
        ) or 0

    context_items = [
        {"label": "PJTに紐づいている", "ok": has_project, "action": None},
        {"label": f"固有名詞辞書（{vocab_count}件）", "ok": vocab_count > 0, "action": None},
        {"label": "MTGの目的が設定されている", "ok": has_purpose, "action": "scroll_mtg"},
        {"label": "アジェンダが設定されている", "ok": has_agenda, "action": "scroll_mtg"},
        {"label": f"参加者情報（{member_count}人）", "ok": member_count > 0, "action": "scroll_mtg"},
    ]
    context_score = sum(1 for c in context_items if c["ok"])

    return JSONResponse({
        "segment_count": len(segments),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "costs": costs,
        "context_items": context_items,
        "context_score": context_score,
        "context_total": len(context_items),
        "has_anthropic": settings.has_anthropic,
    })


@router.post("/api/transcripts/{transcript_id}/polish/pickup")
async def polish_pickup(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """整文前に固有名詞候補をピックアップする。"""
    if not settings.has_anthropic:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY が設定されていません")

    transcript = _get_transcript_or_404(transcript_id, user["id"], db)

    segments = list(db.scalars(
        select(Segment)
        .where(Segment.transcript_id == transcript_id)
        .order_by(Segment.order_index)
    ))
    full_text = "\n".join(s.text_content for s in segments)

    # 登録済み語句
    registered: list[str] = []
    if transcript.project_id:
        vocab = list(db.scalars(
            select(ProjectVocabulary.word)
            .where(ProjectVocabulary.project_id == transcript.project_id)
        ))
        registered = vocab

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    words = await pickup_proper_nouns(client, full_text, registered)

    return JSONResponse({"words": words})


@router.post("/api/transcripts/{transcript_id}/polish/run")
async def polish_run(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: PolishRunRequest,
) -> JSONResponse:
    """整文を実行し、セグメントごとの提案テキストを返す。DBは更新しない。"""
    if not settings.has_anthropic:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY が設定されていません")

    model_key = body.model_key if body.model_key in POLISH_MODELS else "haiku"
    transcript = _get_transcript_or_404(transcript_id, user["id"], db)

    segments = list(db.scalars(
        select(Segment)
        .where(Segment.transcript_id == transcript_id)
        .order_by(Segment.order_index)
    ))
    if not segments:
        raise HTTPException(status_code=400, detail="セグメントがありません")

    context_text = _build_transcript_context(transcript, db)

    # ピックアップ後に追加登録された語句をコンテキストに追加
    if body.extra_vocabulary:
        extra_lines = "\n".join(
            f"  - {v.get('word', '')}: {v.get('meaning', '')}"
            for v in body.extra_vocabulary
        )
        if extra_lines:
            context_text += f"\n【追加固有名詞（今回登録）】\n{extra_lines}"

    # 話者名マップを構築して整文コンテキストに渡す
    speakers_for_map = list(db.scalars(
        select(Speaker).where(Speaker.transcript_id == transcript_id)
    ))
    speaker_name_map = {s.speaker_label: (s.display_name or s.speaker_label) for s in speakers_for_map}

    seg_dicts = [
        {
            "id": s.id,
            "speaker": s.display_name or speaker_name_map.get(s.speaker_label, s.speaker_label),
            "text": s.text_content,
        }
        for s in segments
    ]

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    try:
        result = await run_polish(client, seg_dicts, context_text, model_key)
    except Exception as e:
        logger.error("整文API呼び出しエラー: %s", e)
        raise HTTPException(status_code=502, detail=f"Claude API エラー: {e}")

    # コストログを記録
    log = PolishLog(
        transcript_id=transcript_id,
        model=model_key,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_yen=result.cost_yen,
        created_by_user_id=user["id"],
    )
    db.add(log)

    # last_polished_at を更新
    from datetime import datetime
    transcript.last_polished_at = datetime.now(_tz.utc)
    db.commit()

    return JSONResponse({
        "suggestions": result.suggestions,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_yen": result.cost_yen,
        "model_key": model_key,
    })


@router.post("/api/transcripts/{transcript_id}/polish/accept")
async def polish_accept(
    transcript_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: PolishAcceptRequest,
) -> JSONResponse:
    """採用されたセグメントのテキストをDBに保存する。"""
    transcript = _get_transcript_or_404(transcript_id, user["id"], db)

    updated = 0
    for seg_id, text in body.accepted_texts.items():
        seg = db.get(Segment, seg_id)
        if seg and seg.transcript_id == transcript_id:
            seg.text_content = text
            seg.is_edited = True
            updated += 1

    db.commit()
    return JSONResponse({"ok": True, "updated": updated})


# ── プロフィール ─────────────────────────────────────────────────────────────

@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """プロフィール編集ページ。"""
    db_user = db.get(User, user["id"])
    if not db_user:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    ctx = _common_ctx(user, db)
    ctx["db_user"] = db_user
    return templates.TemplateResponse(request, "profile.html", ctx)


@router.post("/api/profile")
async def update_profile(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: ProfileUpdate,
) -> JSONResponse:
    """会社名・役職を更新する。"""
    db_user = db.get(User, user["id"])
    if not db_user:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    if body.company is not None:
        db_user.company = body.company.strip() or None
    if body.job_title is not None:
        db_user.job_title = body.job_title.strip() or None
    db.commit()
    return JSONResponse({"ok": True, "company": db_user.company, "job_title": db_user.job_title})


# ── People台帳 ────────────────────────────────────────────────────────────────

@router.get("/people", response_class=HTMLResponse)
async def people_page(
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """People台帳管理ページ。"""
    ctx = _common_ctx(user, db)
    return templates.TemplateResponse(request, "people.html", ctx)


@router.get("/api/people")
async def list_people(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """ログインユーザーの People台帳を返す。"""
    rows = db.scalars(
        select(Person)
        .where(Person.owner_user_id == user["id"])
        .order_by(Person.name)
    )
    return JSONResponse([
        {"id": p.id, "name": p.name, "company": p.company or "", "job_title": p.job_title or "", "role": p.role or ""}
        for p in rows
    ])


@router.post("/api/people")
async def create_person(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: PersonCreate,
) -> JSONResponse:
    """People台帳に人物を追加する。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名前は必須です")
    existing = db.scalar(
        select(Person)
        .where(Person.owner_user_id == user["id"], Person.name == name)
    )
    if existing:
        raise HTTPException(status_code=409, detail="同じ名前がすでに登録されています")
    person = Person(
        owner_user_id=user["id"],
        name=name,
        company=body.company.strip() if body.company else None,
        job_title=body.job_title.strip() if body.job_title else None,
        role=body.role.strip() if body.role else None,
    )
    db.add(person)
    db.commit()
    db.refresh(person)
    return JSONResponse({"id": person.id, "name": person.name, "company": person.company or "", "job_title": person.job_title or "", "role": person.role or ""})


@router.put("/api/people/{person_id}")
async def update_person(
    person_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    body: PersonUpdate,
) -> JSONResponse:
    """People台帳の人物情報を更新する。"""
    person = db.scalar(
        select(Person).where(Person.id == person_id, Person.owner_user_id == user["id"])
    )
    if not person:
        raise HTTPException(status_code=404, detail="見つかりません")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="名前は必須です")
        person.name = name
    if body.company is not None:
        person.company = body.company.strip() or None
    if body.job_title is not None:
        person.job_title = body.job_title.strip() or None
    if body.role is not None:
        person.role = body.role.strip() or None
    db.commit()
    return JSONResponse({"id": person.id, "name": person.name, "company": person.company or "", "job_title": person.job_title or "", "role": person.role or ""})


@router.delete("/api/people/{person_id}")
async def delete_person(
    person_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    """People台帳から人物を削除する。"""
    person = db.scalar(
        select(Person).where(Person.id == person_id, Person.owner_user_id == user["id"])
    )
    if not person:
        raise HTTPException(status_code=404, detail="見つかりません")
    db.delete(person)
    db.commit()
    return JSONResponse({"ok": True})
