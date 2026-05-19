"""ストレージ使用量の計算とサマリ取得。

v1.0.2 ストレージ管理機能で使用する。

設計判断（DECISIONS 2026-05-13 参照）:
- ファイルシステムを正典とする（`/data/audio/` を 1 パススキャン）。
  DB の `file_size_bytes` は「ユーザーが音声を手動削除」した後でも残るので、
  実ディスク使用量とずれる可能性があるため信頼しない。
- 1 パススキャンは数千ファイルでも数ミリ秒〜数十ミリ秒で完了するので、
  ホーム画面・詳細画面のリクエストごとに計算する（キャッシュ不要）。

v1.1.1 変更（2026-05-19）:
- ディスク合計・使用量を shutil.disk_usage() で実測するように変更。
  従来の STORAGE_LIMIT_BYTES（手動設定の推定値）は使わない。
  Railway のボリュームサイズを変更しても設定変更なしで警告が正しく動く。
- STORAGE_LIMIT_BYTES が設定されていれば「それ以下に抑えたい上限」として
  min(実ディスク合計, STORAGE_LIMIT_BYTES) を使う。
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from backend.config import load_settings

logger = logging.getLogger(__name__)

# storage.py と同じ場所を参照する。環境変数で上書き可能。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_AUDIO_DIR = _REPO_ROOT / "data" / "audio"
_AUDIO_DIR = Path(os.environ.get("AUDIO_STORAGE_DIR") or str(_DEFAULT_AUDIO_DIR))

# /data が存在しない場合（ローカル開発など）のフォールバック先
_DATA_ROOT = Path(os.environ.get("AUDIO_STORAGE_DIR", "/data")).parent if os.environ.get("AUDIO_STORAGE_DIR") else Path("/data")


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


def compute_audio_used() -> tuple[int, int]:
    """`/data/audio/` 配下の音声ファイル合計バイト数と件数を返す。

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
                        logger.debug("stat 失敗: %s", entry.path)
    except OSError:
        logger.exception("ストレージ使用量のスキャンに失敗: dir=%s", _AUDIO_DIR)
        return 0, 0
    return total, count


# 後方互換エイリアス（routes.py 等から compute_used_bytes として参照していた箇所用）
compute_used_bytes = compute_audio_used


def get_disk_total() -> int:
    """実際のディスク合計容量（バイト）を返す。

    shutil.disk_usage で実測する。取得できなければ 0 を返す。
    ローカル開発など /data が存在しない場合は _AUDIO_DIR の親で代替する。
    """
    for path in [_DATA_ROOT, _AUDIO_DIR, Path(".")]:
        try:
            return shutil.disk_usage(str(path)).total
        except OSError:
            continue
    return 0


def get_effective_limit() -> int:
    """アラート計算に使う「上限バイト数」を返す。

    優先順位:
    1. STORAGE_LIMIT_BYTES が設定されていれば min(実ディスク合計, 設定値) を使う
       → 「5 GB ディスクのうち 2 GB まで」のような運用上の上限を設けられる
    2. 未設定（0）なら実ディスク合計をそのまま使う
    """
    settings = load_settings()
    disk_total = get_disk_total()
    configured = settings.storage_limit_bytes
    if configured > 0 and disk_total > 0:
        return min(configured, disk_total)
    if disk_total > 0:
        return disk_total
    return max(1, configured)  # 両方取れない場合のフォールバック


def _format_bytes_gb(n: int) -> str:
    """バイト数を GB (SI, 10^9) 表記の文字列に整形する。

    1 GB 未満は MB で返す（"312 MB" 等）。
    """
    if n < 0:
        n = 0
    if n < 1_000_000_000:
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
    """現在のストレージ使用量サマリを返す。テンプレート / API レスポンスで使う。

    used_bytes には音声ファイルのみ（DB ファイルを除く）を使う。
    limit_bytes には実際のディスク合計容量を使う（STORAGE_LIMIT_BYTES で上限可）。
    これにより: 「ユーザーが使った音声ファイルが、ディスク全体の何%か」が分かる。
    """
    settings = load_settings()
    used, count = compute_audio_used()
    limit = max(1, get_effective_limit())
    percent = int(used * 100 / limit)
    level = _classify_level(
        percent,
        settings.storage_warning_percent,
        settings.storage_danger_percent,
    )
    return StorageUsageSummary(
        used_bytes=used,
        file_count=count,
        limit_bytes=limit,
        percent=percent,
        used_pretty=_format_bytes_gb(used),
        limit_pretty=_format_bytes_gb(limit),
        level=level,
        over_hard_limit=used >= int(limit * settings.storage_hard_limit_percent / 100),
    )


def get_effective_max_upload_bytes() -> int:
    """実際にアップロード可能な最大ファイルサイズ（バイト）を返す。

    技術的な上限 MAX_UPLOAD_BYTES と実ディスク空き容量の小さい方。
    ディスクの 5% を DB・WAL 書き込み用に確保した残りを上限とする。
    """
    from backend.transcribe.constants import MAX_UPLOAD_BYTES
    try:
        du = shutil.disk_usage(str(_DATA_ROOT))
        # 5% を DB 書き込み用に確保してから残りを上限とする
        safety = max(10 * 1024 * 1024, int(du.total * 0.05))  # 最低 10 MB
        available = max(0, du.free - safety)
        return min(MAX_UPLOAD_BYTES, available)
    except OSError:
        from backend.transcribe.constants import MAX_UPLOAD_BYTES
        return MAX_UPLOAD_BYTES


def would_exceed_hard_limit(additional_bytes: int) -> bool:
    """指定バイトを追加した場合にハードリミットを超えるか判定。

    実際のディスク空き容量も加味する。音声ファイルの計算上は余裕があっても
    ディスク自体が満杯なら拒否する。
    """
    settings = load_settings()
    limit = max(1, get_effective_limit())
    hard_limit = int(limit * settings.storage_hard_limit_percent / 100)

    # 音声ファイルベースのチェック
    used, _ = compute_audio_used()
    if used + additional_bytes > hard_limit:
        return True

    # 実ディスク空き容量チェック（DB などが大きくなってもブロックできる）
    try:
        free = shutil.disk_usage(str(_DATA_ROOT)).free
        # 5% の安全マージン + アップロードサイズ より空きが少なければ拒否
        safety = int(limit * 0.05)
        if free < additional_bytes + safety:
            return True
    except OSError:
        pass

    return False
