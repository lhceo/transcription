"""複数音声ファイルの結合ユーティリティ。

ffmpeg の concat デマルチプレクサを使用して複数ファイルを 1 つの m4a に結合する。
元ファイルの削除は呼び出し元が行うこと。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_FFMPEG_TIMEOUT = 600  # 10分


async def merge_audio_files(paths: list[Path], output_path: Path) -> Path:
    """paths の音声を順番に結合して output_path (m4a) に書き出す。

    1 ファイルの場合はコピーのみ。
    ffmpeg が見つからなければ FileNotFoundError を上げる。
    """
    if not paths:
        raise ValueError("結合するファイルがありません")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(paths) == 1:
        shutil.copy2(str(paths[0]), str(output_path))
        return output_path

    list_path = output_path.parent / f"filelist_{uuid.uuid4().hex[:8]}.txt"
    try:
        with open(list_path, "w", encoding="utf-8") as f:
            for p in paths:
                escaped = str(p.resolve()).replace("\\", "/").replace("'", r"'\''")
                f.write(f"file '{escaped}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c:a", "aac", "-b:a", "128k",
            "-vn",
            str(output_path),
        ]

        logger.info("ffmpeg 結合開始: %d ファイル → %s", len(paths), output_path.name)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("ffmpeg の処理がタイムアウトしました（10分）")

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[-1000:]
            logger.error("ffmpeg 失敗: rc=%d stderr=…%s", proc.returncode, err)
            raise RuntimeError(f"音声ファイルの結合に失敗しました。\n{err}")

        logger.info("ffmpeg 結合完了: %s (%d bytes)", output_path.name, output_path.stat().st_size)
        return output_path
    finally:
        if list_path.exists():
            list_path.unlink(missing_ok=True)
