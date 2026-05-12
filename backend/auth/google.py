"""Google OAuth クライアントの初期化。

authlib の Starlette 統合を使う。OpenID Connect の discovery エンドポイントから
endpoints を自動取得するので、URL をハードコードする必要がない。
"""

from __future__ import annotations

from authlib.integrations.starlette_client import OAuth

from backend.config import load_settings

_settings = load_settings()

oauth = OAuth()

# Google OAuth が設定されている時のみ register する。未設定（開発初期）でも
# サーバーが起動できるようにするため。
if _settings.has_google_oauth:
    oauth.register(
        name="google",
        client_id=_settings.google_oauth_client_id,
        client_secret=_settings.google_oauth_client_secret,
        # Google の OpenID Connect discovery URL。
        # ここから authorize_url / token_url / jwks_uri / userinfo_endpoint が
        # 自動的に取得される。
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            # openid: ID トークンを返してもらう（メール・名前の取得に必要）
            # email / profile: メールアドレスとプロフィール情報
            "scope": "openid email profile",
            # PKCE: モダンな OAuth 2.0 ベストプラクティス
            "code_challenge_method": "S256",
        },
    )
