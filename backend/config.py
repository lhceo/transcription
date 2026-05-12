"""アプリの環境設定を読み込むモジュール。

環境変数 → .env ファイル の順に読み、設定オブジェクトとして公開する。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# プロジェクトルートにある .env を読み込む。CI/本番では環境変数が
# 優先されるので .env が無くても動く。
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env", override=False)

# アプリのバージョン (フッター表示、OpenAPI バージョン、health 応答で使用)。
# リリース時にここだけ書き換えると全ての参照箇所に反映される。
#
# v1.1.0 は ROADMAP 上「音声同期再生 (ハイライト・自動スクロール・クリックで
# シーク)」のマイルストーンとして予約済み。今回のリリースは単純な音声
# プレイヤーまでで同期機能は未達成なので、v1.0.x 系として扱う。
APP_VERSION = "1.0.2"


def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        raise RuntimeError(
            f"環境変数 {key} が設定されていません。.env ファイル "
            "または環境変数で設定してください。"
        )
    return value or ""


@dataclass(frozen=True)
class Settings:
    """アプリ全体で使う設定のスナップショット。"""

    # 環境名（development / production）
    env: str

    # Web サーバの起動設定（ローカル開発時のみ参照）
    host: str
    port: int

    # セッション署名キー（本番では強力なランダム値が必須）
    session_secret: str

    # 許可されたメールドメインのリスト
    allowed_email_domains: tuple[str, ...]

    # Google OAuth 設定（Phase 2 で使用）
    google_oauth_client_id: str
    google_oauth_client_secret: str
    # コールバック URL。本番では正しいドメインを使う必要があるので env で上書き可。
    google_oauth_redirect_uri: str

    # AssemblyAI 設定（Phase 4 で使用）
    assemblyai_api_key: str

    # 月次コスト上限 (円)。0 または未設定の場合は上限なし。
    # 月初は日本時間 0:00 でリセット。Railway の Variables から変更する。
    monthly_cost_limit_yen: int

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def has_google_oauth(self) -> bool:
        """Google OAuth が設定済みか。未設定なら認証機能を無効化する。"""
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret)

    @property
    def has_assemblyai(self) -> bool:
        """AssemblyAI が設定済みか。未設定なら文字起こし機能を無効化する。"""
        return bool(self.assemblyai_api_key)


def load_settings() -> Settings:
    return Settings(
        env=_env("APP_ENV", "development"),
        host=_env("APP_HOST", "127.0.0.1"),
        port=int(_env("APP_PORT", "8000")),
        # 開発用デフォルト。本番では .env か Railway の環境変数で必ず上書きする。
        session_secret=_env(
            "SESSION_SECRET",
            "dev-only-insecure-secret-change-me",
        ),
        allowed_email_domains=tuple(
            d.strip().lower()
            for d in _env("ALLOWED_EMAIL_DOMAINS", "").split(",")
            if d.strip()
        ),
        google_oauth_client_id=_env("GOOGLE_OAUTH_CLIENT_ID", ""),
        google_oauth_client_secret=_env("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        google_oauth_redirect_uri=_env(
            "GOOGLE_OAUTH_REDIRECT_URI",
            "http://127.0.0.1:8000/auth/google/callback",
        ),
        assemblyai_api_key=_env("ASSEMBLYAI_API_KEY", ""),
        monthly_cost_limit_yen=_safe_int(_env("MONTHLY_COST_LIMIT_YEN", "0")),
    )


def _safe_int(value: str) -> int:
    """環境変数の値を int に変換する。無効値は 0 として扱う。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
