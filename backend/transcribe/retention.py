"""自動削除機能 (v1.0.2)。

設計（DECISIONS 2026-05-13 参照）:
- 文字起こし完了から `DATA_RETENTION_DAYS` 日経過したデータを物理削除する
  (音声・テキスト・DB レコード全部、β スコープ)
- 期限判定は `completed_at + retention_days` を毎回計算する
  (カラム追加なし、計算ロジックは本モジュールに集約)
- バックグラウンドタスクで 1 日 1 回スキャン (FastAPI lifespan + asyncio loop)

将来「永久保存」「個別延長」を実装したくなったら `expires_at` カラムを
追加して `transcript_expires_at()` の中身だけ書き換える。テンプレート側や
削除タスクは変更不要にする。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import select

from backend.config import load_settings
from backend.db import SessionLocal
from backend.db.models import Transcript
from backend.transcribe.storage import delete_stored_audio

logger = logging.getLogger(__name__)

# カウントダウン表示の色レベル。テンプレートで CSS クラス名として使う。
ExpiryLevel = Literal["safe", "warning", "danger", "critical", "expired"]


@dataclass(frozen=True)
class ExpiryStatus:
    """文字起こし 1 件の自動削除状態のスナップショット。"""

    expires_at: datetime | None
    # 残り日数 (整数、切り捨て)。None なら判定不能 (未完了など)。
    # 負の値は期限切れ。
    days_left: int | None
    level: ExpiryLevel
    # ダッシュボードで表示すべきか (30 日超なら False)
    show_on_dashboard: bool


def transcript_expires_at(transcript: Any) -> datetime | None:
    """文字起こしの自動削除予定日時を返す。

    - 完了 (`completed_at` あり) かつ retention_days > 0 のときだけ値を返す
    - 未完了 / 自動削除無効化時は None
    """
    completed_at = getattr(transcript, "completed_at", None)
    if completed_at is None:
        return None
    settings = load_settings()
    retention = settings.data_retention_days
    if retention <= 0:
        return None
    return completed_at + timedelta(days=retention)


def _classify_level(days_left: int) -> ExpiryLevel:
    """残り日数から色レベルを判定する。

    UX-2 で確定した閾値:
    - 30 日超: safe (詳細画面のみ控えめ表示)
    - 7〜30 日: warning (黄)
    - 1〜7 日: danger (赤太字)
    - 当日 (0 日): critical (赤、まもなく削除)
    - 負: expired (削除対象、表示時は「期限切れ」)
    """
    if days_left < 0:
        return "expired"
    if days_left == 0:
        return "critical"
    if days_left <= 7:
        return "danger"
    if days_left <= 30:
        return "warning"
    return "safe"


def expiry_status(transcript: Any, *, now: datetime | None = None) -> ExpiryStatus:
    """文字起こしの自動削除状態を返す。テンプレートで `{{ ... }}` で参照する。"""
    expires = transcript_expires_at(transcript)
    if expires is None:
        return ExpiryStatus(
            expires_at=None, days_left=None, level="safe", show_on_dashboard=False
        )
    now = now or datetime.now(timezone.utc)
    # timedelta.days は切り捨てで、過去なら負になる
    delta = expires - now
    # ぴったり 0 を「あと 0 日 = 当日」として扱うため、切り上げ寄りに計算
    # ex: 0.5 日後 → days_left=0 ("まもなく削除"扱い)
    # ex: 1.5 日後 → days_left=1 ("あと 1 日")
    total_seconds = delta.total_seconds()
    days_left = int(total_seconds // (24 * 3600))
    # 過去でなく今日中に消えるなら 0 として扱う
    if total_seconds > 0 and days_left == 0:
        days_left = 0
    level = _classify_level(days_left)
    return ExpiryStatus(
        expires_at=expires,
        days_left=days_left,
        level=level,
        # 30 日超 (level=safe) はダッシュボードでは非表示 (UX-2 確定)
        show_on_dashboard=(level != "safe"),
    )


def _physically_delete_transcript(transcript: Transcript) -> None:
    """1 件を物理削除する。音声ファイル → DB レコード (cascade で segments/speakers も削除)。"""
    tid = transcript.id
    # 音声ファイル削除 (存在しなくても OK)
    try:
        delete_stored_audio(tid)
    except Exception:
        logger.exception("自動削除: 音声ファイル削除失敗 transcript_id=%s", tid)
        # DB 削除は続行する (音声残骸は次回スキャンで再試行される)


def scan_and_delete_expired(*, dry_run: bool = False) -> int:
    """期限切れの文字起こしを物理削除する。削除件数を返す。

    `dry_run=True` で件数だけ確認できる (実際の削除は行わない)。
    """
    settings = load_settings()
    if settings.data_retention_days <= 0:
        logger.info("自動削除: DATA_RETENTION_DAYS=%s なのでスキップ", settings.data_retention_days)
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.data_retention_days)
    deleted = 0

    with SessionLocal() as db:
        # 期限切れの "completed" 文字起こしを取得 (failed / processing は対象外)
        stmt = (
            select(Transcript)
            .where(Transcript.status == "completed")
            .where(Transcript.completed_at.is_not(None))
            .where(Transcript.completed_at < cutoff)
        )
        candidates = list(db.scalars(stmt))
        logger.info(
            "自動削除: 対象 %s 件 (cutoff=%s, retention=%s 日, dry_run=%s)",
            len(candidates),
            cutoff.isoformat(),
            settings.data_retention_days,
            dry_run,
        )

        for t in candidates:
            logger.info(
                "自動削除: transcript_id=%s user_id=%s filename=%s completed_at=%s",
                t.id,
                t.user_id,
                t.original_filename,
                t.completed_at.isoformat() if t.completed_at else "?",
            )
            if dry_run:
                deleted += 1
                continue
            _physically_delete_transcript(t)
            # cascade で segments / speakers も消える (models.py 参照)
            db.delete(t)
            deleted += 1

        if not dry_run:
            db.commit()

    return deleted


# ── バックグラウンドタスク ───────────────────────────────────────────────

# 1 日 1 回スキャンする。Railway のコンテナ単一インスタンス前提なので
# 簡易的に asyncio.sleep でループする。複数インスタンス運用になったら
# 別途調停 (DB ロック / 専用ワーカー) が必要。
_SCAN_INTERVAL_SECONDS = 24 * 60 * 60  # 24 時間

# 起動直後すぐ走らないよう少し待つ。アプリ起動時の他の初期化を邪魔しない。
_INITIAL_DELAY_SECONDS = 5 * 60  # 5 分


async def retention_loop() -> None:
    """FastAPI lifespan から asyncio.create_task で起動する自動削除ループ。

    サーバー再起動でリセットされる (タイマー位置は記憶しない)。
    起動から 5 分後に初回スキャン、その後 24 時間ごと。
    """
    try:
        await asyncio.sleep(_INITIAL_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    while True:
        try:
            deleted = scan_and_delete_expired()
            if deleted > 0:
                logger.info("自動削除: 完了 %s 件削除", deleted)
            else:
                logger.debug("自動削除: 対象なし")
        except Exception:
            # ループが落ちないよう全例外を握る。次回 24 時間後に再試行。
            logger.exception("自動削除タスクで予期せぬ例外")

        try:
            await asyncio.sleep(_SCAN_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
