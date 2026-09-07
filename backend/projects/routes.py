"""プロジェクト管理（大テーマ → プロジェクト）のHTTPルート。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.auth.dependencies import CurrentUser
from backend.config import APP_VERSION, load_settings
from backend.db import get_db
from backend.db.models import Project, ProjectMember, ProjectVocabulary, Theme, Transcript, User
from backend.transcribe.cost import get_cost_summary
from backend.transcribe.display import has_stored_audio, transcript_display_name
from backend.transcribe.retention import expiry_status
from backend.transcribe.storage_usage import get_summary as get_storage_summary

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


class ProfileUpdate(BaseModel):
    company: str | None = None
    job_title: str | None = None


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

    ctx = _common_ctx(user, db)
    ctx.update({
        "project": proj,
        "related_transcripts": related_transcripts,
        "all_users": all_users,
        "member_display_name": _member_display_name,
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

    if body.name is not None:
        member.name = body.name
    if body.company is not None:
        member.company = body.company
    if body.job_title is not None:
        member.job_title = body.job_title
    if body.project_role is not None:
        member.project_role = body.project_role
    db.commit()
    return JSONResponse({"id": member.id, "project_role": member.project_role})


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
    vocab = ProjectVocabulary(project_id=project_id, word=word)
    db.add(vocab)
    db.commit()
    db.refresh(vocab)
    return JSONResponse({"id": vocab.id, "word": vocab.word}, status_code=201)


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
    """アップロードフォームのプロジェクト選択用一覧。"""
    projects = list(
        db.scalars(
            select(Project)
            .options(selectinload(Project.theme))
            .order_by(Project.name)
        )
    )
    result = [
        {
            "id": p.id,
            "name": p.name,
            "theme_name": p.theme.name if p.theme else None,
        }
        for p in projects
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
    vocab_words = [v.word for v in proj.vocabulary]

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
