"""AssemblyAI REST API の薄いクライアント。

公式の Python SDK もあるが、依存を増やさず・挙動を完全制御するために
httpx で直接呼ぶ。

公式 API ドキュメント: https://www.assemblyai.com/docs/api-reference
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiofiles
import httpx

logger = logging.getLogger(__name__)


class AssemblyAIError(Exception):
    """AssemblyAI API 呼び出しで失敗した時に投げる。"""

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AssemblyAIClient:
    """AssemblyAI への非同期クライアント。``async with`` 推奨。"""

    BASE_URL = "https://api.assemblyai.com"

    # 大容量ファイルアップロード用の長めタイムアウト
    UPLOAD_TIMEOUT_SECONDS = 60 * 30  # 30分

    # ポーリング・通常 API は短め
    DEFAULT_TIMEOUT_SECONDS = 60

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("AssemblyAI API キーが空です")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"authorization": api_key},
            timeout=httpx.Timeout(self.DEFAULT_TIMEOUT_SECONDS, connect=10),
        )

    async def __aenter__(self) -> "AssemblyAIClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ── /v2/upload ────────────────────────────────────────────────────────
    async def upload_audio(self, file_path: Path) -> str:
        """音声ファイルを AssemblyAI のストレージにアップロードし、URL を返す。

        2GB ファイルを直接送るので、async ジェネレータでストリーミング送信する。
        """

        async def _file_iter():  # type: ignore[no-untyped-def]
            async with aiofiles.open(file_path, "rb") as f:
                while True:
                    chunk = await f.read(1024 * 1024)  # 1MiB
                    if not chunk:
                        break
                    yield chunk

        logger.info("AssemblyAI へアップロード開始: %s", file_path)
        response = await self._client.post(
            "/v2/upload",
            content=_file_iter(),
            headers={"content-type": "application/octet-stream"},
            timeout=httpx.Timeout(self.UPLOAD_TIMEOUT_SECONDS, connect=10),
        )
        if response.status_code != 200:
            raise AssemblyAIError(
                f"upload failed: {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )
        upload_url = response.json().get("upload_url")
        if not upload_url:
            raise AssemblyAIError("upload response に upload_url がない", body=response.text)
        logger.info("AssemblyAI アップロード完了")
        return upload_url

    # ── /v2/transcript (submit) ────────────────────────────────────────────
    async def submit_transcript(
        self,
        audio_url: str,
        *,
        model_tier: str = "best",
        language_code: str = "ja",
        speakers_expected: int | None = None,
        word_boost: list[str] | None = None,
    ) -> str:
        """文字起こしジョブを投入。AssemblyAI 側の transcript_id を返す。"""
        # B-1 の「best/nano」を AssemblyAI の現行モデル名にマップ:
        # - best → universal（高精度、日本語対応）
        # - nano → nano（コスト重視、日本語対応）
        # 2026年に speech_model（単数・廃止）→ speech_models（複数・配列）に
        # 仕様変更されたので、配列で送信する。
        speech_model = "universal" if model_tier == "best" else "nano"
        body: dict = {
            "audio_url": audio_url,
            "speaker_labels": True,
            "language_code": language_code,
            "speech_models": [speech_model],
        }
        if speakers_expected is not None:
            body["speakers_expected"] = speakers_expected
        if word_boost:
            body["word_boost"] = word_boost
            body["boost_param"] = "high"
        response = await self._client.post("/v2/transcript", json=body)
        if response.status_code not in (200, 201):
            raise AssemblyAIError(
                f"submit transcript failed: {response.status_code} body={response.text[:300]}",
                status_code=response.status_code,
                body=response.text,
            )
        transcript_id = response.json().get("id")
        if not transcript_id:
            raise AssemblyAIError(
                "submit response に id がない", body=response.text
            )
        logger.info("AssemblyAI ジョブ投入: id=%s tier=%s", transcript_id, speech_model)
        return transcript_id

    # ── /v2/transcript/:id (get) ────────────────────────────────────────────
    async def get_transcript(self, transcript_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v2/transcript/{transcript_id}")
        if response.status_code != 200:
            raise AssemblyAIError(
                f"get transcript failed: {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )
        return response.json()

    # ── /v2/transcript/:id (delete) ────────────────────────────────────────
    # 削除はバックグラウンドジョブの最後に呼ばれるクリーンアップ操作。
    # AssemblyAI 障害時にデフォルト 60 秒の読み取りタイムアウトまで待つと、
    # event loop 上のジョブ完了処理が長時間ブロックされる。10 秒で打ち切る。
    DELETE_TIMEOUT_SECONDS = 10

    async def delete_transcript(self, transcript_id: str) -> None:
        """AssemblyAI のサーバーから音声と結果データを削除依頼する。"""
        try:
            response = await self._client.delete(
                f"/v2/transcript/{transcript_id}",
                timeout=httpx.Timeout(self.DELETE_TIMEOUT_SECONDS, connect=5),
            )
        except httpx.TimeoutException:
            logger.warning(
                "AssemblyAI 削除タイムアウト: id=%s (%ss で打ち切り)",
                transcript_id,
                self.DELETE_TIMEOUT_SECONDS,
            )
            return
        if response.status_code not in (200, 204):
            # 削除失敗はログだけ残して握りつぶす（処理は完了させたい）
            logger.warning(
                "AssemblyAI 削除失敗: id=%s status=%s body=%s",
                transcript_id,
                response.status_code,
                response.text[:200],
            )
            return
        logger.info("AssemblyAI 削除完了: id=%s", transcript_id)
