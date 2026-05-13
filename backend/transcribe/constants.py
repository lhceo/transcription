"""文字起こしまわりの定数。

DESIGN.md の D-5（ファイルサイズ上限 2GB）と A-1（対応形式）に対応。
v1.0.3 で対応形式を拡張: iPhone ボイスメモの .m4a、iPhone カメラの
.mov、高音質志向の .wav にも対応。AssemblyAI 側はこれらすべてに対応
しているので、許可リストを広げるだけで動く。
"""

from __future__ import annotations

# 上限: 2GB
MAX_UPLOAD_BYTES: int = 2 * 1024 * 1024 * 1024

# 受け入れる拡張子（小文字、ドット付き）。
# 主要なスマホ録音形式をカバーする:
# - mp3: 既存サポート、汎用
# - mp4: 既存サポート、動画付き録音
# - m4a: iPhone ボイスメモ標準形式 (Android 主要メーカーも多くが採用)
# - wav: 高音質志向、ディクテーション用アプリ
# - mov: iPhone カメラの標準動画コンテナ
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".mp4", ".m4a", ".wav", ".mov"}
)

# 拡張子と HTTP 応答時の Content-Type のマッピング。GET /api/transcripts/
# {id}/audio で `<audio>` / `<video>` 要素が正しく再生できるよう、ファイル
# 拡張子に応じた MIME タイプを返す。
MEDIA_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",  # MP4 コンテナの音声トラックのみ
    ".wav": "audio/wav",
    ".mov": "video/quicktime",
}

# MIME タイプ（参考。クライアント送信値は信頼しないので、補助的に検証）。
# Safari / Chrome / Firefox それぞれで微妙に違う MIME を送ってくる可能性が
# あるので、メジャーな別表記も許容する。
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/x-m4a",
        "audio/wav",
        "audio/wave",
        "audio/x-wav",
        "video/mp4",
        "video/quicktime",
        # ブラウザによっては別の MIME を送ってくる場合もある
        "application/octet-stream",
    }
)


def format_label_for_user() -> str:
    """エンドユーザーに見せる「対応形式」の表記。help / dropzone hint で使う。"""
    return "mp3 / mp4 / m4a / wav / mov"
