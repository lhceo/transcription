"""処理時間の目安計算。

AssemblyAI の経験則として「処理時間 ≈ 音声長 / factor」で見積もる。
factor は実運用フィードバックを見ながら調整する想定の初期値。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# best / nano の係数（音声長 / 係数 = おおよその処理秒数）
_PROCESSING_FACTOR: dict[str, float] = {
    "best": 15.0,
    "nano": 25.0,
}


def compute_eta_text(transcript: Any) -> str | None:
    """進行中ジョブの「あと約 X 分」テキストを返す。

    完了済み・失敗、または音声長未確定なら None を返す（テンプレート側で
    None 判定して表示を切り替える前提）。
    """
    status = getattr(transcript, "status", None)
    if status not in ("uploaded", "processing"):
        return None

    duration = getattr(transcript, "audio_duration_seconds", None)
    if not duration or duration <= 0:
        return None

    tier = getattr(transcript, "model_tier", "best") or "best"
    factor = _PROCESSING_FACTOR.get(tier, _PROCESSING_FACTOR["best"])
    total_estimate_seconds = duration / factor

    created_at = getattr(transcript, "created_at", None)
    if created_at is None:
        minutes = max(1, math.ceil(total_estimate_seconds / 60))
        return f"あと約 {minutes} 分"

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - created_at).total_seconds()
    remaining = total_estimate_seconds - elapsed

    if remaining <= 60:
        return "もうすぐ完了"
    minutes = math.ceil(remaining / 60)
    return f"あと約 {minutes} 分"
