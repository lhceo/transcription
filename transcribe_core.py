"""
Core transcription logic: audio extraction -> Whisper -> pyannote diarization.

Follows the logic of the original transcribe.py:
  - diarization.serialize()["diarization"]  (not itertracks)
  - Consecutive same-speaker segments are merged with a full-width space
  - Default speaker label is "\u4e0d\u660e" when no overlap is found
  - HF token read from HF_TOKEN env var (UI value takes precedence if set)
"""

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional


def _resource_dir() -> Path:
    """Return the directory containing app resources (works both in development
    and inside a py2app .app bundle)."""
    # py2app sets RESOURCEPATH to <app>.app/Contents/Resources
    res = os.environ.get("RESOURCEPATH")
    if res and Path(res).exists():
        return Path(res)
    return Path(__file__).resolve().parent


def _ffmpeg_executable() -> str:
    """Locate ffmpeg: prefer a binary bundled with the app, fall back to PATH."""
    bundled = _resource_dir() / "bundled" / "ffmpeg" / "ffmpeg"
    if bundled.is_file() and os.access(str(bundled), os.X_OK):
        return str(bundled)
    return "ffmpeg"


def _is_model_cached(model_id: str) -> bool:
    """Best-effort check for whether a HuggingFace model is already in the
    user's local cache. Used to decide whether to warn about a slow first run."""
    cache_root = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    hub = cache_root / "hub"
    if not hub.is_dir():
        return False
    safe_name = "models--" + model_id.replace("/", "--")
    repo = hub / safe_name
    if not repo.is_dir():
        return False
    snaps = repo / "snapshots"
    if not snaps.is_dir():
        return False
    # If at least one snapshot directory contains files, treat as cached.
    for entry in snaps.iterdir():
        if entry.is_dir() and any(entry.iterdir()):
            return True
    return False


@dataclass
class Segment:
    start: float
    end: float
    speaker: str          # original pyannote label, e.g. "SPEAKER_01"
    text: str
    display_name: str = ""  # editable label shown in UI (empty = use speaker)

    def label(self) -> str:
        return self.display_name if self.display_name else self.speaker


def format_time(seconds: float) -> str:
    """MM:SS or HH:MM:SS, no decimals."""
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def segments_to_txt(segments: list) -> str:
    return "\n\n".join(
        f"[{format_time(seg.start)} {seg.label()}]\n{seg.text}"
        for seg in segments
    )


def segments_to_srt(segments: list) -> str:
    def _t(s: float) -> str:
        h, rem = divmod(int(s), 3600)
        m, sec = divmod(rem, 60)
        ms = int((s % 1) * 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    blocks = []
    for i, seg in enumerate(segments, 1):
        blocks.append(
            f"{i}\n"
            f"{_t(seg.start)} --> {_t(seg.end)}\n"
            f"[{seg.label()}] {seg.text}\n"
        )
    return "\n".join(blocks)


def segments_to_json(segments: list) -> str:
    return json.dumps(
        [
            {
                "start": seg.start,
                "end": seg.end,
                "speaker": seg.speaker,
                "display_name": seg.display_name,
                "text": seg.text,
            }
            for seg in segments
        ],
        ensure_ascii=False,
        indent=2,
    )


def _extract_audio(input_path: str, output_path: str) -> None:
    """Convert any audio/video to 16 kHz mono WAV for Whisper."""
    if not os.path.exists(input_path):
        raise FileNotFoundError(
            f"音声ファイルが見つかりません:\n{input_path}\n"
            "ファイルが移動・削除されている可能性があります。"
        )
    ffmpeg = _ffmpeg_executable()
    cmd = [
        ffmpeg, "-i", input_path,
        "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le",
        "-y", output_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        bundled_hint = (
            "アプリに ffmpeg が同梱されていません。"
            "再インストールするか、開発者にお問い合わせください。"
        ) if ffmpeg != "ffmpeg" else (
            "ffmpeg が見つかりません。\n"
            "ターミナルで `brew install ffmpeg` を実行してインストールしてください。\n"
            "（既にインストール済みの場合は PATH の設定を確認してください）"
        )
        raise RuntimeError(bundled_hint) from exc
    except OSError as exc:
        raise RuntimeError(f"ffmpeg の実行に失敗しました: {exc}") from exc
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{r.stderr[-2000:]}")


def _best_speaker(seg_start: float, seg_end: float, diar_segments: list) -> str:
    """
    Return the pyannote speaker label with maximum overlap.
    Returns "\u4e0d\u660e" when no overlap is found.
    """
    best_spk = "\u4e0d\u660e"
    best_overlap = 0.0
    for d in diar_segments:
        overlap = min(seg_end, d["end"]) - max(seg_start, d["start"])
        if overlap > best_overlap:
            best_overlap = overlap
            best_spk = d["speaker"]
    return best_spk


_JP_PUNCT_END = "\u3002\uff0e\u3001\uff01\uff1f!?\uff09\u300d\u300f"


def _join_segment_text(prev: str, nxt: str) -> str:
    """Join two consecutive Whisper segments without inserting visible whitespace
    when Japanese punctuation or CJK characters already imply a boundary."""
    if not prev:
        return nxt
    last = prev[-1]
    if last in _JP_PUNCT_END:
        return prev + nxt
    if (
        "\u3040" <= last <= "\u30ff"   # hiragana / katakana
        or "\u4e00" <= last <= "\u9fff"  # CJK unified ideographs
        or "\uff00" <= last <= "\uffef"  # full-width forms
    ):
        return prev + nxt
    return prev + " " + nxt


def _merge_consecutive(raw: list) -> list:
    """Merge consecutive same-speaker entries into a single segment."""
    merged = []
    for speaker, start, end, text in raw:
        if merged and merged[-1][0] == speaker:
            merged[-1][2] = end
            merged[-1][3] = _join_segment_text(merged[-1][3], text)
        else:
            merged.append([speaker, start, end, text])

    return [
        Segment(start=m[1], end=m[2], speaker=m[0], text=m[3])
        for m in merged
    ]


class TranscriptionEngine:
    """Stateful engine; caches the pyannote pipeline between runs."""

    def __init__(self) -> None:
        self._pipeline = None
        self._pipeline_token: Optional[str] = None

    def _load_pipeline(self, hf_token: str) -> None:
        if self._pipeline is not None and self._pipeline_token == hf_token:
            return
        try:
            from pyannote.audio import Pipeline  # type: ignore
            import torch  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "話者分離に必要なライブラリ (pyannote.audio / torch) が"
                "見つかりません。再インストールが必要です。"
            ) from exc

        try:
            self._pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=hf_token,
            )
        except Exception as exc:
            raise RuntimeError(
                "話者分離モデルの読み込みに失敗しました。\n"
                "・HuggingFace Token が正しいか確認してください\n"
                "・ネットワーク接続を確認してください\n"
                f"（詳細: {exc}）"
            ) from exc

        # MPS (Apple Silicon GPU) を試し、使えなければ CPU フォールバック。
        # Intel Mac やバージョン非互換のときも落ちないように。
        try:
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                self._pipeline = self._pipeline.to(torch.device("mps"))
        except Exception:
            pass  # CPU で続行
        self._pipeline_token = hf_token

    def transcribe(
        self,
        file_path: str,
        model: str = "mlx-community/whisper-large-v3-mlx",
        language: Optional[str] = None,
        use_diarization: bool = True,
        hf_token: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
    ) -> list:
        def _progress(msg: str, pct: int) -> None:
            if progress_callback:
                progress_callback(msg, pct)

        if not os.path.exists(file_path):
            raise FileNotFoundError(
                f"\u6307\u5b9a\u3055\u308c\u305f\u30d5\u30a1\u30a4\u30eb\u304c\u898b\u3064\u304b\u308a\u307e\u305b\u3093:\n{file_path}"
            )

        token = hf_token or os.environ.get("HF_TOKEN") or None
        if use_diarization and not token:
            raise RuntimeError(
                "\u8a71\u8005\u5206\u96e2\u3092\u4f7f\u3046\u306b\u306f HuggingFace Token \u304c\u5fc5\u8981\u3067\u3059\u3002\n"
                "\u30fb\u8a2d\u5b9a\u3067 HuggingFace Token \u3092\u5165\u529b\u3059\u308b\u304b\n"
                "\u30fb\u30bf\u30fc\u30df\u30ca\u30eb\u3067 `export HF_TOKEN=hf_xxx` \u3092\u5b9f\u884c\u3057\u3066\u304b\u3089\n"
                "  \u30a2\u30d7\u30ea\u3092\u518d\u8d77\u52d5\u3057\u3066\u304f\u3060\u3055\u3044\u3002"
            )

        _progress("\u97f3\u58f0\u3092\u62bd\u51fa\u4e2d\u2026", 5)

        # tempfile.TemporaryDirectory() \u306f\u901a\u5e38 /tmp \u306a\u3069\u3092\u4f7f\u3046\u304c\u3001\u30b5\u30f3\u30c9\u30dc\u30c3\u30af\u30b9\u3084
        # \u66f8\u304d\u8fbc\u307f\u6a29\u9650\u306e\u306a\u3044\u74b0\u5883\u3067\u5931\u6557\u3059\u308b\u3053\u3068\u304c\u3042\u308b\u305f\u3081\u3001~/.transcription_app/tmp
        # \u306b\u660e\u793a\u30d5\u30a9\u30fc\u30eb\u30d0\u30c3\u30af\u3002
        tmp_root = None
        try:
            tmp_ctx = tempfile.TemporaryDirectory()
        except (OSError, PermissionError):
            tmp_root = os.path.join(
                os.path.expanduser("~"), ".transcription_app", "tmp"
            )
            os.makedirs(tmp_root, exist_ok=True)
            tmp_ctx = tempfile.TemporaryDirectory(dir=tmp_root)

        with tmp_ctx as tmpdir:
            audio_path = os.path.join(tmpdir, "audio.wav")
            _extract_audio(file_path, audio_path)

            diar_segments: list = []
            if use_diarization and token:
                if _is_model_cached("pyannote/speaker-diarization-3.1"):
                    _progress("\u8a71\u8005\u5206\u96e2\u30e2\u30c7\u30eb\u3092\u8aad\u307f\u8fbc\u307f\u4e2d\u2026", 12)
                else:
                    _progress(
                        "\u8a71\u8005\u5206\u96e2\u30e2\u30c7\u30eb\u3092\u30c0\u30a6\u30f3\u30ed\u30fc\u30c9\u4e2d\uff08\u521d\u56de\u306e\u307f\uff09\u2026", 12
                    )
                self._load_pipeline(token)
                _progress("\u8a71\u8005\u5206\u96e2\u3092\u5b9f\u884c\u4e2d\u2026", 20)
                diarization = self._pipeline(audio_path)
                diar_segments = diarization.serialize()["diarization"]

            if _is_model_cached(model):
                _progress("\u6587\u5b57\u8d77\u3053\u3057\u3092\u5b9f\u884c\u4e2d\u2026", 50)
            else:
                _progress(
                    "\u6587\u5b57\u8d77\u3053\u3057\u30e2\u30c7\u30eb\u3092\u30c0\u30a6\u30f3\u30ed\u30fc\u30c9\u4e2d\uff08\u521d\u56de\u306e\u307f\u30fb\u7d043GB\u30015\u301c15\u5206\u304b\u304b\u308a\u307e\u3059\uff09\u2026",
                    50,
                )
            import mlx_whisper  # type: ignore

            whisper_kwargs: dict = {
                "path_or_hf_repo": model,
                "word_timestamps": True,
            }
            if language:
                whisper_kwargs["language"] = language

            result = mlx_whisper.transcribe(audio_path, **whisper_kwargs)

            _progress("\u8a71\u8005\u3092\u5272\u308a\u5f53\u3066\u4e2d\u2026", 92)
            raw: list = []
            for seg in result.get("segments", []):
                text = seg["text"].strip()
                if not text:
                    continue
                start, end = seg["start"], seg["end"]
                speaker = _best_speaker(start, end, diar_segments) if diar_segments else "SPEAKER_00"
                raw.append((speaker, start, end, text))

            segments = _merge_consecutive(raw)

            _progress("\u5b8c\u4e86", 100)
            return segments
