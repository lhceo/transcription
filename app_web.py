"""
Flask + PyWebView transcription app.
Run:  python app_web.py
Deps: pip install flask pywebview
"""

import json
import os
import threading
import time

from flask import Flask, jsonify, render_template, request, send_from_directory

from transcribe_core import (
    Segment,
    TranscriptionEngine,
    format_time,
    segments_to_json,
    segments_to_srt,
    segments_to_txt,
)

app = Flask(__name__, static_folder="frontend", template_folder="frontend")

_engine = TranscriptionEngine()

_state: dict = {
    "segments": [],
    "current_file": None,
    "speaker_names": {},
    "running": False,
    "progress_msg": "",
    "progress_pct": 0,
    "error": None,
}

_SPEAKER_COLORS = [
    "#3B82F6", "#10B981", "#F59E0B", "#EF4444",
    "#8B5CF6", "#EC4899", "#06B6D4", "#84CC16",
]


def _speaker_color(label: str) -> str:
    idx = abs(hash(label)) % len(_SPEAKER_COLORS)
    return _SPEAKER_COLORS[idx]


def _segments_to_api(segments: list[Segment]) -> list[dict]:
    return [
        {
            "index": i,
            "start": seg.start,
            "end": seg.end,
            "start_fmt": format_time(seg.start),
            "speaker": seg.speaker,
            "display_name": seg.display_name,
            "label": seg.display_name if seg.display_name else seg.speaker,
            "color": _speaker_color(seg.display_name if seg.display_name else seg.speaker),
            "text": seg.text,
        }
        for i, seg in enumerate(segments)
    ]


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("frontend", "index.html")


@app.route("/api/state")
def api_state():
    return jsonify({
        "running": _state["running"],
        "progress_msg": _state["progress_msg"],
        "progress_pct": _state["progress_pct"],
        "error": _state["error"],
        "current_file": _state["current_file"],
        "has_segments": len(_state["segments"]) > 0,
    })


@app.route("/api/segments")
def api_segments():
    return jsonify(_segments_to_api(_state["segments"]))


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    if _state["running"]:
        return jsonify({"error": "既に実行中です"}), 409

    data = request.json or {}
    file_path = data.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "ファイルが見つかりません"}), 400

    model = data.get("model", "mlx-community/whisper-large-v3-mlx")
    language = data.get("language") or None
    use_diarization = bool(data.get("use_diarization", True))
    hf_token = data.get("hf_token") or os.environ.get("HF_TOKEN") or None

    _state["running"] = True
    _state["progress_msg"] = "開始中…"
    _state["progress_pct"] = 0
    _state["error"] = None
    _state["segments"] = []
    _state["current_file"] = os.path.basename(file_path)
    _state["speaker_names"] = {}

    def _run():
        try:
            def _cb(msg: str, pct: int):
                _state["progress_msg"] = msg
                _state["progress_pct"] = pct

            segs = _engine.transcribe(
                file_path,
                model=model,
                language=language,
                use_diarization=use_diarization,
                hf_token=hf_token,
                progress_callback=_cb,
            )
            _state["segments"] = segs
        except Exception as exc:
            _state["error"] = str(exc)
        finally:
            _state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/rename", methods=["POST"])
def api_rename():
    data = request.json or {}
    old_label = data.get("old_label", "")
    new_label = data.get("new_label", "").strip()
    if not new_label:
        return jsonify({"error": "名前が空です"}), 400
    for seg in _state["segments"]:
        current = seg.display_name if seg.display_name else seg.speaker
        if current == old_label:
            seg.display_name = new_label
    return jsonify({"ok": True})


@app.route("/api/edit", methods=["POST"])
def api_edit():
    data = request.json or {}
    idx = data.get("index")
    new_text = data.get("text", "").strip()
    if idx is None or not (0 <= idx < len(_state["segments"])):
        return jsonify({"error": "不正なインデックス"}), 400
    _state["segments"][idx].text = new_text
    return jsonify({"ok": True})


@app.route("/api/export", methods=["POST"])
def api_export():
    data = request.json or {}
    fmt = data.get("format", "txt")
    segs = _state["segments"]
    if not segs:
        return jsonify({"error": "データがありません"}), 400

    if fmt == "txt":
        content = segments_to_txt(segs)
        ext = "txt"
        mime = "text/plain"
    elif fmt == "srt":
        content = segments_to_srt(segs)
        ext = "srt"
        mime = "text/plain"
    elif fmt == "json":
        content = segments_to_json(segs)
        ext = "json"
        mime = "application/json"
    else:
        return jsonify({"error": "不明なフォーマット"}), 400

    return jsonify({"content": content, "ext": ext, "mime": mime})


@app.route("/api/project/save", methods=["POST"])
def api_project_save():
    segs = _state["segments"]
    if not segs:
        return jsonify({"error": "データがありません"}), 400
    data = {
        "version": 1,
        "current_file": _state["current_file"],
        "segments": [
            {"start": s.start, "end": s.end, "speaker": s.speaker,
             "display_name": s.display_name, "text": s.text}
            for s in segs
        ],
    }
    return jsonify({"content": json.dumps(data, ensure_ascii=False, indent=2)})


@app.route("/api/project/load", methods=["POST"])
def api_project_load():
    data = request.json or {}
    content = data.get("content", "")
    try:
        proj = json.loads(content)
        segs = [
            Segment(
                start=s["start"], end=s["end"],
                speaker=s["speaker"], text=s["text"],
                display_name=s.get("display_name", ""),
            )
            for s in proj.get("segments", [])
        ]
        _state["segments"] = segs
        _state["current_file"] = proj.get("current_file")
        _state["running"] = False
        _state["error"] = None
        return jsonify({"ok": True, "count": len(segs)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/copy_text")
def api_copy_text():
    segs = _state["segments"]
    return jsonify({"text": segments_to_txt(segs) if segs else ""})


# ─── PyWebView file-dialog API ─────────────────────────────────────────────────

class _FileApi:
    """Methods exposed to JS via window.pywebview.api.*"""

    def select_audio_file(self):
        import webview  # type: ignore
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Audio/Video (*.mp3;*.mp4;*.m4a;*.wav;*.aac;*.flac;*.ogg;*.webm;*.mov;*.mkv)",),
        )
        if result:
            path = result[0]
            return {"path": path, "name": os.path.basename(path)}
        return None

    def save_file(self, filename: str, content: str, ext: str):
        import webview  # type: ignore
        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=filename,
        )
        if result:
            save_path = result if isinstance(result, str) else result[0]
            if not save_path.endswith(f".{ext}"):
                save_path += f".{ext}"
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"path": save_path}
        return None

    def open_project_file(self):
        import webview  # type: ignore
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Project (*.transcription.json)",),
        )
        if result:
            path = result[0]
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return {"path": path, "content": content}
        return None

    def save_project_file(self, content: str):
        import webview  # type: ignore
        base = _state.get("current_file") or "project"
        name = os.path.splitext(base)[0] + ".transcription.json"
        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=name,
        )
        if result:
            save_path = result if isinstance(result, str) else result[0]
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"path": save_path}
        return None


# ─── Entry point ───────────────────────────────────────────────────────────────

def main():
    import webview  # type: ignore

    def _start_flask():
        import logging
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        app.run(host="127.0.0.1", port=5001, threaded=True, use_reloader=False)

    t = threading.Thread(target=_start_flask, daemon=True)
    t.start()
    time.sleep(0.8)

    webview.create_window(
        "文字起こしアプリ",
        "http://127.0.0.1:5001",
        js_api=_FileApi(),
        width=1200,
        height=800,
        min_size=(900, 600),
    )
    webview.start()


if __name__ == "__main__":
    main()
