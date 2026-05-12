"""認証関連の FastAPI 依存関数。

ルート関数に `current_user: dict = Depends(require_user)` のように
書くだけで、未認証時は /login にリダイレクトされる。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse


class SessionUser(TypedDict):
    """セッション Cookie に格納するユーザー情報の最小セット。"""

    id: int  # DB の users.id
    email: str
    name: str
    picture: str
    sub: str  # Google の一意な ID（メール変更にも追従できるので保存しておく）


def get_optional_user(request: Request) -> SessionUser | None:
    """セッションからユーザー情報を取り出す（未認証なら None）。

    旧バージョンのセッション（id が無い）はここで無効化する。
    結果として再ログインが促される。
    """
    user = request.session.get("user")
    if isinstance(user, dict) and "id" in user and "email" in user:
        return user  # type: ignore[return-value]
    # 旧セッション or 未認証 → 古いセッションをクリアして無効化
    if user is not None:
        request.session.clear()
    return None


def require_user(request: Request) -> SessionUser:
    """認証されていないと /login にリダイレクトする依存。

    Depends() で使う想定。RedirectResponse を raise したいが FastAPI の
    Depends は戻り値を期待するので、`HTTPException` ではなく
    `RedirectResponse` を直接 raise する代わりに、例外を投げて
    main.py 側の exception_handler でリダイレクトする方法もある。
    ここではシンプルに、未認証なら /login へリダイレクトする
    RedirectResponse を「擬似的に return」できるよう、route 側で
    `if isinstance(result, RedirectResponse): return result` する
    パターンを取らず、別の方法を採用する：

    実装方針: 未認証なら `_RedirectToLogin` という独自例外を投げて、
    main.py の exception_handler で `/login` にリダイレクトする。
    """
    user = get_optional_user(request)
    if user is None:
        raise _RedirectToLogin()
    return user


class _RedirectToLogin(Exception):
    """未認証アクセス時に投げる内部例外。main.py の handler で捕捉する。"""


CurrentUser = Annotated[SessionUser, Depends(require_user)]
OptionalUser = Annotated[SessionUser | None, Depends(get_optional_user)]
