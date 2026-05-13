"""ストレージ使用量の計算とサマリ取得。

v1.0.2 ストレージ管理機能で使用する。

設計判断（DECISIONS 2026-05-13 参照）:
- ファイルシステムを正典とする（`/data/audio/` を 1 パススキャン）。
  DB の `file_size_bytes` は「ユーザーが音声を手動削除」した後でも残るので、
  実ディスク使用量とずれる可能性があるため信頼しない。
- 1 パススキャンは数千ファイルでも数ミリ秒〜数十ミリ秒で完了するので、
  ホーム画面・詳細画面のリクエストごとに計算する（キャッシュ不要）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from backend.config import load_settings

logger = logging.getLogger(__name__)

# storage.py と同じ場所を参照する。環境変数で上書き可能。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_AUDIO_DIR = _REPO_ROOT / "data" / "audio"
_AUDIO_DIR = Path(os.environ.get("AUDIO_STORAGE_DIR") or str(_DEFAULT_AUDIO_DIR))


UsageLevel = Literal["ok", "warning", "danger"]


@dataclass(frozen=True)
class StorageUsageSummary:
    """ストレージ使用量のスナップショット。テンプレートに渡しやすい形。"""

    used_bytes: int
    file_count: int
    limit_bytes: int
    # 使用率 (0-100 の int)。100 を超える場合もそのまま返す（運用エラー検知用）。
    percent: int
    # 表示用テキスト（"3.6 GB" 等）。GB は SI (10^9 バイト) で計算。
    used_pretty: str
    limit_pretty: str
    # 状態。"ok" / "warning" / "danger" の 3 段階。
    level: UsageLevel
    # ハードリミット（アップロード拒否境界）を超えているか
    over_hard_limit: bool

    @property
    def is_warning(self) -> bool:
        return self.level in ("warning", "danger")

    @property
    def is_danger(self) -> bool:
        return self.level == "danger"


def compute_used_bytes() -> tuple[int, int]:
    """`/data/audio/` 配下のファイル合計バイト数と件数を返す。

    シンボリックリンクは追わない、サブディレクトリも見ない（フラットな配置前提）。
    """
    if not _AUDIO_DIR.exists():
        return 0, 0
    total = 0
    count = 0
    try:
        with os.scandir(_AUDIO_DIR) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False):
                    try:
                        total += entry.stat(follow_symlinks=False).st_size
                        count += 1
                    except OSError:
                        # ファイルが消えた・権限なし等は無視（ログだけ）
                        logger.debug("stat 失敗: %s", entry.path)
    except OSError:
        logger.exception("ストレージ使用量のスキャンに失敗: dir=%s", _AUDIO_DIR)
        return 0, 0
    return total, count


def _format_bytes_gb(n: int) -> str:
    """バイト数を GB (SI, 10^9) 表記の文字列に整形する。

    1 GB 未満は MB で返す（"312 MB" 等）。
    """
    if n < 0:
        n = 0
    if n < 1_000_000_000:
        # 1 GB 未満は MB 表記
        mb = n / 1_000_000
        return f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"
    gb = n / 1_000_000_000
    return f"{gb:.1f} GB"


def _classify_level(
    percent: int,
    warning_percent: int,
    danger_percent: int,
) -> UsageLevel:
    if percent >= danger_percent:
        return "danger"
    if percent >= warning_percent:
        return "warning"
    return "ok"


def get_summary() -> StorageUsageSummary:
    """現在のストレージ使用量サマリを返す。テンプレート / API レスポンスで使う。"""
    settings = load_settings()
    used, count = compute_used_bytes()
    limit = max(1, settings.storage_limit_bytes)  # 0 除算回避
    percent = int(used * 100 / limit)
    level = _classify_level(
        percent,
        settings.storage_warning_percent,
        settings.storage_danger_percent,
    )
    return StorageUsageSummary(
        used_bytes=used,
        file_count=count,
        limit_bytes=settings.storage_limit_bytes,
        percent=percent,
        used_pretty=_format_bytes_gb(used),
        limit_pretty=_format_bytes_gb(settings.storage_limit_bytes),
        level=level,
        over_hard_limit=used >= settings.storage_hard_limit_bytes,
    )


def would_exceed_hard_limit(additional_bytes: int) -> bool:
    """指定バイトを追加した場合にハードリミットを超えるか判定。

    アップロード前のサーバー側ガードで使う。受信開始時の Content-Length と
    現在使用量から、超過予想なら 413 を返す判断に。
    """
    settings = load_settings()
    used, _ = compute_used_bytes()
    return (used + additional_bytes) > settings.storage_hard_limit_bytes
