"""文字起こしコストの計算。

単価は AssemblyAI の料金と為替次第で変動するため、ハードコードした
概算値（経験則）を使う。実利用後に調整しやすいよう 1 箇所にまとめる。

月次の集計タイムゾーンは Asia/Tokyo（日本の業務感覚に合わせる）。
DB 上の日時は UTC で保存されているので、月初の境界を UTC に変換して
比較する。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import Transcript

# 1 時間あたりの概算コスト (円)。実料金 / 為替に応じて調整。
COST_YEN_PER_HOUR: dict[str, int] = {
    "best": 55,
    "nano": 20,
}

# 月次集計の基準タイムゾーン
JST = ZoneInfo("Asia/Tokyo")


def estimate_cost_yen(duration_seconds: float | None, tier: str) -> int:
    """1 ジョブのコストを推定する。

    duration_seconds が未確定 (None / 0) の場合は 0 を返す。
    """
    if not duration_seconds or duration_seconds <= 0:
        return 0
    rate = COST_YEN_PER_HOUR.get(tier, COST_YEN_PER_HOUR["best"])
    hours = duration_seconds / 3600.0
    return max(1, int(math.ceil(hours * rate)))


def month_start_utc(now_utc: datetime | None = None) -> datetime:
    """日本時間で見た「今月の月初 0:00」を UTC datetime で返す。

    DB の created_at は UTC なので、UTC で比較できる形にして返す。
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    first_of_month_jst = now_jst.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return first_of_month_jst.astimezone(timezone.utc)


def current_month_cost_yen(db: Session, now_utc: datetime | None = None) -> int:
    """今月（日本時間ベース）の累計コストを返す。

    内訳:
    - 完了済みジョブ: `cost_yen` をそのまま合計
    - 進行中ジョブ (uploaded / processing): `audio_duration_seconds` と
      `model_tier` から推定値を加算。未確定なら 0 として扱う

    削除済み (deleted_at != None) は集計対象外。
    失敗 (failed) も対象外（請求されないので）。
    """
    start = month_start_utc(now_utc)

    stmt = (
        select(Transcript)
        .where(Transcript.created_at >= start)
        .where(Transcript.deleted_at.is_(None))
        .where(Transcript.status.in_(("uploaded", "processing", "completed")))
    )
    total = 0
    for t in db.scalars(stmt):
        if t.status == "completed":
            total += int(t.cost_yen or 0)
        else:
            total += estimate_cost_yen(t.audio_duration_seconds, t.model_tier or "best")
    return total


def will_exceed_limit(
    db: Session,
    duration_seconds: float | None,
    tier: str,
    limit_yen: int,
    *,
    safety_margin: float = 0.10,
) -> tuple[bool, int, int]:
    """新規アップロードを受け入れたら月次上限を超えるか判定する。

    戻り値: (超える? , 推定追加コスト, 今月の現累計)

    safety_margin はクライアント送信の duration がやや過小評価でも
    対応できるよう、推定値に上乗せする比率（既定 10%）。
    """
    if limit_yen <= 0:
        return False, 0, 0  # 上限なし
    base_estimate = estimate_cost_yen(duration_seconds, tier)
    estimate_with_margin = int(math.ceil(base_estimate * (1.0 + safety_margin)))
    current = current_month_cost_yen(db)
    return (current + estimate_with_margin > limit_yen, estimate_with_margin, current)


def next_month_start_jst_text(now_utc: datetime | None = None) -> str:
    """次月の月初日時を「YYYY年MM月DD日 (X曜日)」風の日本語で返す。

    エラーメッセージの「いつリセットされるか」表示用。
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    if now_jst.month == 12:
        next_month = now_jst.replace(year=now_jst.year + 1, month=1, day=1)
    else:
        next_month = now_jst.replace(month=now_jst.month + 1, day=1)
    next_month = next_month.replace(hour=0, minute=0, second=0, microsecond=0)
    weekday = "月火水木金土日"[next_month.weekday()]
    return f"{next_month.year}年{next_month.month}月{next_month.day}日 ({weekday})"
