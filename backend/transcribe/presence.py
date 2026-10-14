"""インメモリのリアルタイム編集プレゼンス管理。

ユーザーが現在どのセグメントを編集しているかを TTL 付きで保持する。
サーバー再起動でリセットされる（ポーリングで再登録される）。
"""

from __future__ import annotations

from datetime import datetime, timezone

# 構造: {transcript_id: {user_id: {name, picture_url, segment_id, updated_at}}}
_presence: dict[int, dict[int, dict]] = {}

PRESENCE_TTL = 30  # seconds


def update(
    transcript_id: int,
    user_id: int,
    user_name: str,
    picture_url: str | None,
    segment_id: int | None,
) -> None:
    """プレゼンスを更新（セグメント編集開始・継続）。segment_id=None で離脱扱い。"""
    if transcript_id not in _presence:
        _presence[transcript_id] = {}
    _presence[transcript_id][user_id] = {
        "name": user_name,
        "picture_url": picture_url,
        "segment_id": segment_id,
        "updated_at": datetime.now(timezone.utc).timestamp(),
    }


def clear(transcript_id: int, user_id: int) -> None:
    """ユーザーのプレゼンスを削除（明示的離脱）。"""
    if transcript_id in _presence:
        _presence[transcript_id].pop(user_id, None)


def get_active(transcript_id: int) -> list[dict]:
    """TTL 切れを除去して、現在アクティブなプレゼンス一覧を返す。

    Returns:
        [{"user_id": int, "name": str, "picture_url": str|None, "segment_id": int|None}, ...]
    """
    now = datetime.now(timezone.utc).timestamp()
    bucket = _presence.get(transcript_id, {})

    # TTL 切れを除去
    expired = [uid for uid, info in bucket.items() if now - info["updated_at"] > PRESENCE_TTL]
    for uid in expired:
        del bucket[uid]

    return [
        {
            "user_id": uid,
            "name": info["name"],
            "picture_url": info["picture_url"],
            "segment_id": info["segment_id"],
        }
        for uid, info in bucket.items()
    ]
