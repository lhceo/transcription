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
import tempfile
from dataclasses import asdict, dataclass
from typing import Callable, Optional


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
    cmd = [
        "ffmpeg", "-i", input_path,
        "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le",
        "-y", output_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
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


def _merge_consecutive(raw: list) -> list:
    """
    Merge consecutive same-speaker entries, joining text with a full-width space (U+3000).
    """
    merged = []
    for speaker, start, end, text in raw:
        if merged and merged[-1][0] == speaker:
            merged[-1][2] = end
            merged[-1][3] = merged[-1][3] + "\u3000" + text
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
        from pyannote.audio import Pipeline  # type: ignore
        import torch  # type: ignore

        self._pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=hf_token,
        )
        if torch.backends.mps.is_available():
            self._pipeline = self._pipeline.to(torch.device("mps"))
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

        token = hf_token or os.environ.get("HF_TOKEN") or None

        _progress("\u97f3\u58f0\u3092\u62bd\u51fa\u4e2d\u2026", 5)

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "audio.wav")
            _extract_audio(file_path, audio_path)

            diar_segments: list = []
            if use_diarization and token:
                _progress("\u8a71\u8005\u5206\u96e2\u30e2\u30c7\u30eb\u3092\u8aad\u307f\u8fbc\u307f\u4e2d\u2026", 12)
                self._load_pipeline(token)
                _progress("\u8a71\u8005\u5206\u96e2\u3092\u5b9f\u884c\u4e2d\u2026", 20)
                diarization = self._pipeline(audio_path)
                diar_segments = diarization.serialize()["diarization"]

            _progress("\u6587\u5b57\u8d77\u3053\u3057\u3092\u5b9f\u884c\u4e2d\u2026", 50)
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
