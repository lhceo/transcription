"""認証関連のモジュール。

- `google` : Google OAuth クライアントの初期化
- `routes` : FastAPI のルーター（/login, /auth/google, /auth/google/callback, /auth/logout）
- `dependencies` : 認証が必要なエンドポイント用の依存関数
"""

from backend.auth.routes import router

__all__ = ["router"]
