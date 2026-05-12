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

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"


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
            d.strip()
            for d in _env("ALLOWED_EMAIL_DOMAINS", "").split(",")
            if d.strip()
        ),
    )
