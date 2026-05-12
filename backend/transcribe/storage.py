"""音声ファイルの保存・取得・削除。

設計方針（DECISIONS.md 2026-05-12 A-4 改訂）:
- アップロード受信時は一時領域に書き込み、AssemblyAI への送信を経た後で
  永続ボリュームへ移動する
- 永続側はユーザーが「音声を聞いて思い出す」「v1.1 で同期再生する」目的で
  保管。手動削除のみ（自動削除は別タスクで追加予定）
- 一時領域はサーバー再起動で消えてもよい（処理中の音声のみ置く）
- 永続領域は Railway のボリューム /data 下にマウントされる前提
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

import aiofiles
from fastapi import UploadFile

from backend.transcribe.constants import MAX_UPLOAD_BYTES

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ──── 一時アップロード保存先 ─────────────────────────────────────────
# 処理中のみ存在する。AssemblyAI に送り終わったら永続側に移動 (またはコピー)
# する。デフォルトはリポジトリ直下 tmp/uploads/、本番では UPLOAD_TMP_DIR で
# /tmp/transcription_uploads などを指定する。
_DEFAULT_UPLOADS = _REPO_ROOT / "tmp" / "uploads"
_UPLOADS_DIR = Path(os.environ.get("UPLOAD_TMP_DIR") or str(_DEFAULT_UPLOADS))
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ──── 永続音声保管先 ──────────────────────────────────────────────
# Railway では AUDIO_STORAGE_DIR=/data/audio を設定する。ローカル開発は
# リポジトリ直下 data/audio に置く（.gitignore 済）。
_DEFAULT_AUDIO_DIR = _REPO_ROOT / "data" / "audio"
_AUDIO_DIR = Path(os.environ.get("AUDIO_STORAGE_DIR") or str(_DEFAULT_AUDIO_DIR))
_AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def audio_storage_path(transcript_id: int, suffix: str) -> Path:
    """永続側の音声ファイルのパスを返す。

    suffix は ".mp3" / ".mp4" 等 (先頭ピリオドありの拡張子)。
    """
    s = suffix.lower()
    if not s.startswith("."):
        s = "." + s
    return _AUDIO_DIR / f"{transcript_id}{s}"


def find_stored_audio(transcript_id: int) -> Path | None:
    """transcript_id に紐づく永続音声ファイルを探す。

    拡張子が事前に分からなくても見つけられるよう、`{id}.*` を glob で探索。
    存在しなければ None。
    """
    matches = list(_AUDIO_DIR.glob(f"{transcript_id}.*"))
    return matches[0] if matches else None


def move_to_storage(source: Path, transcript_id: int) -> Path:
    """一時パスの音声を永続側に移動する。元ファイルは消える。

    既に同じ id のファイルがあれば上書きする (再アップロード時の保険)。
    """
    target = audio_storage_path(transcript_id, source.suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    shutil.move(str(source), str(target))
    logger.info("音声を永続保管に移動: transcript_id=%s path=%s", transcript_id, target)
    return target


def delete_stored_audio(transcript_id: int) -> bool:
    """永続側の音声を削除する。存在しなければ False を返す。"""
    p = find_stored_audio(transcript_id)
    if p is None:
        return False
    try:
        p.unlink()
        logger.info("音声を永続保管から削除: transcript_id=%s path=%s", transcript_id, p)
        return True
    except Exception:
        logger.exception("音声削除に失敗: transcript_id=%s", transcript_id)
        return False


class UploadTooLargeError(Exception):
    """アップロード途中で上限を超えた場合に投げる。"""

    def __init__(self, bytes_read: int) -> None:
        super().__init__(
            f"アップロードファイルが上限 {MAX_UPLOAD_BYTES} バイトを超えました"
            f"（{bytes_read} バイト時点で打ち切り）"
        )
        self.bytes_read = bytes_read


async def save_upload_to_tmp(upload: UploadFile, *, max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[Path, int]:
    """``UploadFile`` をディスクに保存し、保存先パスと総バイト数を返す。

    - メモリにロードせずチャンク単位で書き込む（2GB ファイルでも安全）
    - ``max_bytes`` を超えた時点で書き込みを中断し、部分ファイルを削除する
    """
    job_dir = _UPLOADS_DIR / str(uuid.uuid4())
    job_dir.mkdir(parents=True, exist_ok=True)

    # 元ファイル名は保存に使わない（後で DB に保存する分には参照する）。
    # 拡張子だけ拝借して保存名は固定。
    suffix = Path(upload.filename or "").suffix.lower()
    save_path = job_dir / f"audio{suffix}"

    bytes_written = 0
    chunk_size = 1024 * 1024  # 1MiB

    try:
        async with aiofiles.open(save_path, "wb") as f:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise UploadTooLargeError(bytes_written)
                await f.write(chunk)
    except UploadTooLargeError:
        # 上限超過したら部分ファイルとディレクトリごと削除
        cleanup_job_dir(job_dir)
        raise
    except Exception:
        cleanup_job_dir(job_dir)
        raise

    logger.info(
        "アップロード保存完了: filename=%s size_bytes=%s path=%s",
        upload.filename,
        bytes_written,
        save_path,
    )
    return save_path, bytes_written


def cleanup_job_dir(job_dir: Path) -> None:
    """ジョブディレクトリを削除する（音声ファイルの即削除に使う）。"""
    try:
        shutil.rmtree(job_dir, ignore_errors=False)
    except Exception as exc:
        logger.warning("ジョブディレクトリ削除に失敗: %s (%s)", job_dir, exc)
