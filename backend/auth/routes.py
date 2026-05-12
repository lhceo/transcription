"""認証ルート定義。

- GET  /login                   : ログイン画面（未認証ユーザー向け）
- GET  /auth/google             : Google OAuth フロー開始
- GET  /auth/google/callback    : Google からのコールバック受信
- POST /auth/logout             : ログアウト
"""

from __future__ import annotations

import logging
from pathlib import Path

from authlib.integrations.base_client import OAuthError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from backend.auth.google import oauth
from backend.config import APP_VERSION, load_settings
from backend.db import SessionLocal
from backend.db.models import User

logger = logging.getLogger(__name__)

settings = load_settings()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse, name="login")
async def login(request: Request, error: str | None = None) -> HTMLResponse:
    """ログイン画面。

    既にログイン済みなら / にリダイレクト。
    クエリ `?error=...` でエラー表示できる。
    """
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=302)

    # ログイン時のエラーメッセージ（クエリパラメータから）
    error_messages = {
        "forbidden_domain": (
            "社内アカウントのみご利用いただけます。"
            "別のアカウントでログインしてください。"
        ),
        "oauth_failed": "Google ログインに失敗しました。もう一度お試しください。",
        "email_not_verified": "メールアドレスが確認されていません。",
        "missing_setup": (
            "Google OAuth の設定が完了していません。"
            "管理者にお問い合わせください。"
        ),
    }

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "app_version": APP_VERSION,
            "env": settings.env,
            "error_message": error_messages.get(error),
            "oauth_ready": settings.has_google_oauth,
        },
    )


@router.get("/auth/google", name="auth_google")
async def auth_google(request: Request) -> RedirectResponse:
    """Google OAuth フローを開始する。"""
    if not settings.has_google_oauth:
        return RedirectResponse(url="/login?error=missing_setup", status_code=302)

    # コールバック URL を環境変数から構築（authlib にそのまま渡す）
    redirect_uri = settings.google_oauth_redirect_uri

    # authlib が state/nonce を生成・管理してくれる
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/google/callback", name="auth_google_callback")
async def auth_google_callback(request: Request) -> RedirectResponse:
    """Google からのコールバックを受信し、セッションを確立する。"""
    if not settings.has_google_oauth:
        return RedirectResponse(url="/login?error=missing_setup", status_code=302)

    try:
        # コードを ID トークンに交換し、state を検証
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("OAuth コールバックで失敗: %s", exc)
        return RedirectResponse(url="/login?error=oauth_failed", status_code=302)

    # userinfo（ID トークンに含まれる）を取り出す
    user_info = token.get("userinfo")
    if not user_info:
        logger.warning("ID トークンに userinfo が含まれていない")
        return RedirectResponse(url="/login?error=oauth_failed", status_code=302)

    email = (user_info.get("email") or "").lower().strip()
    email_verified = user_info.get("email_verified")
    if not email or not email_verified:
        return RedirectResponse(
            url="/login?error=email_not_verified", status_code=302
        )

    # ドメイン制限のチェック
    domain = email.split("@")[-1]
    if settings.allowed_email_domains and domain not in settings.allowed_email_domains:
        logger.info("許可されていないドメインからのログイン試行: %s", email)
        return RedirectResponse(url="/login?error=forbidden_domain", status_code=302)

    name = user_info.get("name", email)
    picture = user_info.get("picture", "")
    sub = user_info.get("sub", "")

    # DB にユーザーを upsert（初回ログインなら作成、既存なら名前/画像/最終ログイン更新）
    from datetime import datetime, timezone

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        now = datetime.now(timezone.utc)
        if user is None:
            user = User(
                email=email,
                name=name,
                picture_url=picture or None,
                created_at=now,
                last_login_at=now,
            )
            db.add(user)
        else:
            user.name = name
            user.picture_url = picture or None
            user.last_login_at = now
        db.commit()
        db.refresh(user)
        user_id = user.id

    # セッションにユーザー情報を保存
    request.session["user"] = {
        "id": user_id,
        "email": email,
        "name": name,
        "picture": picture,
        "sub": sub,
    }

    logger.info("ログイン成功: %s", email)
    return RedirectResponse(url="/", status_code=302)


@router.post("/auth/logout", name="auth_logout")
async def auth_logout(request: Request) -> RedirectResponse:
    """ログアウト（セッション破棄）。"""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
