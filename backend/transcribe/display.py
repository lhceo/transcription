"""文字起こしの表示用ユーティリティ。

リネーム機能で `title` カラムが追加されたが、テンプレート / API レスポンス /
エクスポートのファイル名生成など多箇所で「ユーザーに見せる名前」が必要に
なる。優先順位を 1 箇所に集約しておく。
"""

from __future__ import annotations

from typing import Any


def transcript_display_name(transcript: Any) -> str:
    """文字起こしの表示名を返す。

    優先順位: title (ユーザーリネーム) → original_filename (アップロード時の
    元ファイル名)。両方未設定なら空文字。
    """
    title = getattr(transcript, "title", None)
    if title:
        title = str(title).strip()
        if title:
            return title
    return getattr(transcript, "original_filename", "") or ""
