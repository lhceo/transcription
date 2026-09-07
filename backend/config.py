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
# 「リリースタグを切る前に必ずバンプする」を運用ルールとする。
# 過去の経緯:
# - v1.0 リリース (2026-05-12)
# - v1.0.1 (コード掃除 + ドキュメント整合)
# - v1.0.2 (自動削除 + ストレージ管理)
# - v1.0.3 (スマホ対応 + ダッシュボードリネーム) ← ここでバンプ漏れ
# - v1.1 (音声同期再生 + インラインエディタ) ← ここでもバンプ漏れ
APP_VERSION = "1.3.0"


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

    # 自動削除までの保管日数 (v1.0.2)。完了から N 日経過した文字起こしを
    # 音声・テキスト・DB レコードごと物理削除する。0 で自動削除無効。
    data_retention_days: int

    # 管理者メールアドレス。このメールアドレスのユーザーのみ /admin にアクセスできる。
    admin_email: str

    # ストレージ上限 (バイト)。Railway Hobby Plan の 5 GB を default に。
    # GB は SI 表記 (10^9) で扱う。プラン変更や Pro 移行時にここを上げる。
    storage_limit_bytes: int

    # 使用率の閾値 (%)。WARNING ≤ DANGER ≤ HARD_LIMIT の順で大きくなる前提。
    # WARNING: 黄色バナー表示開始。DANGER: 赤バナー表示開始。
    # HARD_LIMIT: アップロード受付を拒否する境界。
    storage_warning_percent: int
    storage_danger_percent: int
    storage_hard_limit_percent: int

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def storage_hard_limit_bytes(self) -> int:
        """アップロード拒否の境界 (バイト)。"""
        return int(self.storage_limit_bytes * self.storage_hard_limit_percent / 100)

    @property
    def storage_warning_bytes(self) -> int:
        """黄色バナー表示の境界 (バイト)。"""
        return int(self.storage_limit_bytes * self.storage_warning_percent / 100)

    @property
    def storage_danger_bytes(self) -> int:
        """赤バナー表示の境界 (バイト)。"""
        return int(self.storage_limit_bytes * self.storage_danger_percent / 100)

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
        admin_email=_env("ADMIN_EMAIL", "a.ichikawa@lionheart.co.jp").lower(),
        monthly_cost_limit_yen=_safe_int(_env("MONTHLY_COST_LIMIT_YEN", "0")),
        # v1.0.2 自動削除 + ストレージ管理機能の設定。default は
        # Railway Hobby Plan (5 GB) と DECISIONS 2026-05-13 で確定した
        # 60 日保管 / 70% 警告 / 90% 危険 / 95% アップロード拒否を使う。
        data_retention_days=_safe_int(_env("DATA_RETENTION_DAYS", "60")),
        storage_limit_bytes=_safe_int(
            _env("STORAGE_LIMIT_BYTES", str(5_000_000_000))
        ),
        storage_warning_percent=_clamp_percent(
            _env("STORAGE_WARNING_PERCENT", "70")
        ),
        storage_danger_percent=_clamp_percent(
            _env("STORAGE_DANGER_PERCENT", "90")
        ),
        storage_hard_limit_percent=_clamp_percent(
            _env("STORAGE_HARD_LIMIT_PERCENT", "95")
        ),
    )


def _safe_int(value: str) -> int:
    """環境変数の値を int に変換する。無効値は 0 として扱う。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _clamp_percent(value: str) -> int:
    """0〜100 にクランプした int を返す。無効値は 0。"""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0
