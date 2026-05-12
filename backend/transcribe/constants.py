"""文字起こしまわりの定数。

DESIGN.md の D-5（ファイルサイズ上限 2GB）と A-1（mp3/mp4 のみ）に対応。
"""

from __future__ import annotations

# 上限: 2GB
MAX_UPLOAD_BYTES: int = 2 * 1024 * 1024 * 1024

# 受け入れる拡張子（小文字、ドット付き）。
# AssemblyAI は他の形式も受けるが、配布前段階では mp3 と mp4 のみサポートする
# 方針（DESIGN.md A-1）。
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".mp4"})

# MIME タイプ（参考。クライアント送信値は信頼しないので、補助的に検証）
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "video/mp4",
        # ブラウザによっては別の MIME を送ってくる場合もある
        "application/octet-stream",
    }
)
