"""音声ファイルの一時保存とクリーンアップ。

設計方針（DESIGN.md A-4）:
- 音声ファイルは AssemblyAI に送り終わったら **即削除** する
- そのため、ここでは「保存」と「削除」のみ提供し、永続化はしない
- パスはサーバー再起動で変わらないよう、リポジトリ外（または明示パス）に固定
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import aiofiles
from fastapi import UploadFile

from backend.transcribe.constants import MAX_UPLOAD_BYTES

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 一時アップロード保存先。設計方針上、処理完了後に必ず削除するので、
# 永続化は不要。デフォルトはリポジトリ直下 tmp/uploads/、本番 (Railway 等)
# では UPLOAD_TMP_DIR=/tmp/transcription_uploads のように env で上書きする。
_DEFAULT_UPLOADS = _REPO_ROOT / "tmp" / "uploads"
_UPLOADS_DIR = Path(os.environ.get("UPLOAD_TMP_DIR") or str(_DEFAULT_UPLOADS))
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


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
