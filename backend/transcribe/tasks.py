"""文字起こし処理のバックグラウンドタスク。

設計方針（DESIGN.md E-1）:
- アップロードと AssemblyAI 投入後、ポーリングはバックグラウンドで実行
- ユーザーがブラウザを閉じても処理は継続（asyncio タスクとして実行）
- サーバー再起動した場合は失われる（将来課題：再開ロジックを実装）

ジョブ ID（assemblyai_transcript_id）は DB に保存しているので、
サーバー再起動後に「processing 状態のジョブ」をスキャンして resume する
ことは可能。Phase 4 ではそこまでやらない。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from backend.config import load_settings
from backend.db import SessionLocal
from backend.db.models import Segment, Speaker, Transcript
from backend.transcribe.assemblyai_client import AssemblyAIClient, AssemblyAIError
from backend.transcribe.storage import cleanup_job_dir

logger = logging.getLogger(__name__)

# ポーリング間隔と最大時間
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 60 * 60 * 6  # 6時間（極端な保険、通常はもっと早く完了）

# コスト試算（1時間あたりの円、為替・AssemblyAI 料金次第で変動）
COST_YEN_PER_HOUR = {
    "best": 55,
    "nano": 20,
}


def _normalize_speaker_label(aai_label: str, label_map: dict[str, str]) -> str:
    """AssemblyAI の話者ラベル（A, B, C...）を SPEAKER_00, SPEAKER_01... に変換。

    label_map は呼び出し側が用意する dict。ここで in-place に更新する。
    """
    if aai_label not in label_map:
        idx = len(label_map)
        label_map[aai_label] = f"SPEAKER_{idx:02d}"
    return label_map[aai_label]


def _save_results_to_db(transcript_id: int, aai_result: dict) -> None:
    """AssemblyAI の結果を segments / speakers / transcripts に反映する。

    呼び出し元は asyncio で動いているが、DB アクセスは同期セッションを
    使うので、関数自体は同期で OK（SQLAlchemy の sync engine 利用）。
    """
    utterances = aai_result.get("utterances") or []
    duration_seconds = aai_result.get("audio_duration") or 0
    if isinstance(duration_seconds, (int, float)):
        duration_seconds_f = float(duration_seconds)
    else:
        duration_seconds_f = 0.0

    label_map: dict[str, str] = {}  # AssemblyAI のラベル -> 我々のラベル

    with SessionLocal() as db:
        transcript = db.get(Transcript, transcript_id)
        if transcript is None:
            logger.error("結果保存時に transcript=%s が見つからない", transcript_id)
            return

        # Segments
        for idx, utt in enumerate(utterances):
            aai_speaker = utt.get("speaker") or "A"
            speaker_label = _normalize_speaker_label(aai_speaker, label_map)

            # AssemblyAI は ms 単位、我々は秒で保持する
            start_ms = utt.get("start") or 0
            end_ms = utt.get("end") or 0
            seg = Segment(
                transcript_id=transcript_id,
                order_index=idx,
                start_seconds=float(start_ms) / 1000.0,
                end_seconds=float(end_ms) / 1000.0,
                speaker_label=speaker_label,
                text_content=(utt.get("text") or "").strip(),
            )
            db.add(seg)

        # Speakers（未設定話者N の初期表示名）
        for n, (_aai_label, our_label) in enumerate(label_map.items(), start=1):
            spk = Speaker(
                transcript_id=transcript_id,
                speaker_label=our_label,
                display_name=f"未設定話者{n}",
            )
            db.add(spk)

        # Transcript 本体を completed に更新
        transcript.status = "completed"
        transcript.audio_duration_seconds = duration_seconds_f
        transcript.completed_at = datetime.now(timezone.utc)

        # コスト試算
        hours = duration_seconds_f / 3600.0
        rate = COST_YEN_PER_HOUR.get(transcript.model_tier, COST_YEN_PER_HOUR["best"])
        transcript.cost_yen = max(1, int(round(hours * rate))) if duration_seconds_f > 0 else 0

        db.commit()
        logger.info(
            "結果保存完了: transcript_id=%s segments=%s speakers=%s cost=%s",
            transcript_id,
            len(utterances),
            len(label_map),
            transcript.cost_yen,
        )


async def _mark_failed(transcript_id: int, error_message: str) -> None:
    """ジョブを failed 状態にマークする。"""
    with SessionLocal() as db:
        t = db.get(Transcript, transcript_id)
        if t is not None:
            t.status = "failed"
            t.error_message = error_message
            db.commit()


async def process_transcript(transcript_id: int, audio_path: Path) -> None:
    """1ジョブのライフサイクル全体を処理する。

    成功・失敗・例外いずれの場合も:
    - ローカルの音声ファイルを削除
    - AssemblyAI 側のデータも削除依頼
    する責務を持つ。
    """
    settings = load_settings()
    if not settings.has_assemblyai:
        logger.error(
            "ASSEMBLYAI_API_KEY 未設定のため処理スキップ: transcript_id=%s",
            transcript_id,
        )
        await _mark_failed(transcript_id, "AssemblyAI API キーが設定されていません")
        cleanup_job_dir(audio_path.parent)
        return

    aai_transcript_id: str | None = None
    client = AssemblyAIClient(settings.assemblyai_api_key)

    try:
        # 1. アップロード
        upload_url = await client.upload_audio(audio_path)

        # 2. DB からモデル設定を取得 + ジョブ投入
        with SessionLocal() as db:
            t = db.get(Transcript, transcript_id)
            if t is None:
                logger.error("transcript=%s が見つからない", transcript_id)
                return
            model_tier = t.model_tier
            language = t.language

        aai_transcript_id = await client.submit_transcript(
            upload_url,
            model_tier=model_tier,
            language_code=language,
        )

        # 3. processing 状態に更新
        with SessionLocal() as db:
            t = db.get(Transcript, transcript_id)
            if t is not None:
                t.status = "processing"
                t.assemblyai_transcript_id = aai_transcript_id
                db.commit()

        # 4. ポーリング
        loop = asyncio.get_event_loop()
        start = loop.time()
        result: dict | None = None
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            res = await client.get_transcript(aai_transcript_id)
            status = res.get("status")
            if status == "completed":
                result = res
                break
            if status == "error":
                err = res.get("error", "AssemblyAI で不明なエラーが発生しました")
                logger.warning("AssemblyAI error: %s", err)
                await _mark_failed(transcript_id, str(err))
                return
            elapsed = loop.time() - start
            if elapsed > POLL_TIMEOUT_SECONDS:
                logger.error(
                    "ポーリングタイムアウト: transcript_id=%s aai_id=%s",
                    transcript_id,
                    aai_transcript_id,
                )
                await _mark_failed(
                    transcript_id,
                    "処理がタイムアウトしました（6時間を超えました）",
                )
                return
            logger.debug(
                "poll: transcript_id=%s aai_id=%s status=%s elapsed=%ss",
                transcript_id,
                aai_transcript_id,
                status,
                int(elapsed),
            )

        # 5. 結果保存（同期処理。少しの間 event loop が止まるが segments 数千件
        #    程度なので問題ない範囲）
        _save_results_to_db(transcript_id, result)

    except AssemblyAIError as exc:
        logger.exception("AssemblyAI 呼び出しで失敗: %s", exc)
        await _mark_failed(transcript_id, f"AssemblyAI エラー: {exc}")
    except Exception as exc:
        logger.exception("バックグラウンド処理で予期せぬ例外: transcript_id=%s", transcript_id)
        await _mark_failed(transcript_id, f"内部エラー: {exc!s}")
    finally:
        # ローカル音声ファイル削除（成功・失敗いずれも実行）
        try:
            cleanup_job_dir(audio_path.parent)
        except Exception:
            logger.exception("ローカル音声ファイル削除に失敗")

        # AssemblyAI から削除依頼（成功・失敗いずれも実行）
        if aai_transcript_id:
            try:
                await client.delete_transcript(aai_transcript_id)
            except Exception:
                logger.exception("AssemblyAI 側の削除に失敗")

        await client.close()
