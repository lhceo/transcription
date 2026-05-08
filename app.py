#!/usr/bin/env python3
"""
Transcription GUI App
Drag-and-drop audio/video → speaker-diarized transcript (mlx-whisper + pyannote)

Planned future features (data structures already in place):
  - Audio playback synced to transcript (segments carry start/end timestamps)
  - Inline text editing
  - Speaker renaming (Segment.display_name + App._speaker_names)
"""

import json
import os
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk  # type: ignore

from transcribe_core import (
    Segment,
    TranscriptionEngine,
    format_time,
    segments_to_json,
    segments_to_srt,
    segments_to_txt,
)

# ─── Drag-and-drop (optional) ─────────────────────────────────────────────────
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore

    _HAS_DND = True
    _AppBase: type = TkinterDnD.Tk
except ImportError:
    _HAS_DND = False
    _AppBase = ctk.CTk  # type: ignore

# ─── Constants ─────────────────────────────────────────────────────────────────
_CONFIG_PATH = Path.home() / ".transcription_app" / "config.json"

_MODELS = [
    ("large-v3  (高精度・標準)", "mlx-community/whisper-large-v3-mlx"),
    ("large-v3-turbo  (高速)", "mlx-community/whisper-large-v3-turbo"),
    ("medium", "mlx-community/whisper-medium-mlx"),
    ("small", "mlx-community/whisper-small-mlx"),
    ("base", "mlx-community/whisper-base-mlx"),
    ("tiny", "mlx-community/whisper-tiny-mlx"),
]

_LANGUAGES = [
    ("自動検出", None),
    ("日本語", "ja"),
    ("English", "en"),
    ("中文", "zh"),
    ("한국어", "ko"),
    ("Français", "fr"),
    ("Deutsch", "de"),
    ("Español", "es"),
]

_ACCEPTED_EXTS = {
    ".mp3", ".mp4", ".m4a", ".wav", ".aiff", ".aac",
    ".ogg", ".flac", ".mov", ".avi", ".mkv", ".webm", ".wma",
}

# 15 distinct colours — modern Tailwind palette, one per display-name label
_SPEAKER_COLORS = [
    "#3B82F6",  # blue
    "#10B981",  # emerald
    "#F59E0B",  # amber
    "#EF4444",  # red
    "#8B5CF6",  # violet
    "#06B6D4",  # cyan
    "#F97316",  # orange
    "#EC4899",  # pink
    "#6366F1",  # indigo
    "#14B8A6",  # teal
    "#84CC16",  # lime
    "#A855F7",  # purple
    "#0EA5E9",  # sky
    "#D946EF",  # fuchsia
    "#78716C",  # warm-gray
]

# Light-theme palette
_BG_LEFT    = "#FFFFFF"
_BG_RIGHT   = "#F1F5F9"
_BG_CARD    = "#F8FAFC"
_ACCENT     = "#3B82F6"
_ACCENT_HOV = "#2563EB"
_BORDER     = "#E2E8F0"
_TEXT       = "#1E293B"
_TEXT_MUTED = "#94A3B8"

_LEFT_WIDTH = 300


# ─── Config persistence ────────────────────────────────────────────────────────

def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ─── Upload icon (Canvas) ──────────────────────────────────────────────────────

def _make_upload_icon(parent, size: int = 52) -> tk.Canvas:
    """Clean upload icon: circle + up-arrow + base line."""
    c = tk.Canvas(parent, width=size, height=size,
                  bg=_BG_LEFT, highlightthickness=0)
    p, col, w = 5, _ACCENT, 2
    c.create_oval(p, p, size - p, size - p, outline=col, width=w)
    cx, cy = size // 2, size // 2
    c.create_line(cx, cy + 10, cx, cy - 7,  fill=col, width=w, capstyle="round")
    c.create_line(cx - 7, cy - 1,  cx, cy - 8,  fill=col, width=w, capstyle="round")
    c.create_line(cx + 7, cy - 1,  cx, cy - 8,  fill=col, width=w, capstyle="round")
    c.create_line(cx - 8, cy + 10, cx + 8, cy + 10, fill=col, width=w, capstyle="round")
    return c


# ─── Drop Zone widget ──────────────────────────────────────────────────────────

class DropZone(ctk.CTkFrame):
    """Visual file-drop target. Falls back to Browse button when DnD is unavailable."""

    def __init__(self, master, on_file: callable, **kwargs):
        kwargs.setdefault("fg_color", _BG_LEFT)
        super().__init__(master, **kwargs)
        self._on_file = on_file
        self._set_border_normal()

        icon = _make_upload_icon(self, size=52)
        icon.pack(pady=(18, 4))

        ctk.CTkLabel(
            self,
            text="ここにファイルをドロップ" if _HAS_DND else "ファイルを選択してください",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=_TEXT,
        ).pack()

        if _HAS_DND:
            ctk.CTkLabel(
                self, text="または",
                font=ctk.CTkFont(size=11),
                text_color=_TEXT_MUTED,
            ).pack(pady=1)

        ctk.CTkButton(
            self, text="ファイルを選択",
            width=130, height=32, corner_radius=8,
            fg_color=_ACCENT, hover_color=_ACCENT_HOV,
            font=ctk.CTkFont(size=12),
            command=self._browse,
        ).pack(pady=4)

        ctk.CTkLabel(
            self,
            text="MP3 · MP4 · WAV · M4A · MOV · その他",
            font=ctk.CTkFont(size=10),
            text_color=_TEXT_MUTED,
        ).pack(pady=(2, 10))

        self._file_label = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=11),
            text_color=_TEXT, wraplength=260,
        )
        self._file_label.pack(pady=(0, 8))

        if _HAS_DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
            self.dnd_bind("<<DragEnter>>", lambda _e: self._set_border_hover())
            self.dnd_bind("<<DragLeave>>", lambda _e: self._set_border_normal())

    def _set_border_normal(self):
        self.configure(border_width=1, border_color=_BORDER)

    def _set_border_hover(self):
        self.configure(border_width=2, border_color=_ACCENT)

    def _browse(self):
        exts = " ".join(f"*{e}" for e in sorted(_ACCEPTED_EXTS))
        path = filedialog.askopenfilename(
            filetypes=[("音声・動画ファイル", exts), ("すべてのファイル", "*.*")]
        )
        if path:
            self._accept(path)

    def _on_drop(self, event):
        self._set_border_normal()
        path = event.data.strip().strip("{}")  # braces appear on macOS
        self._accept(path)

    def _accept(self, path: str):
        if Path(path).suffix.lower() not in _ACCEPTED_EXTS:
            messagebox.showwarning(
                "非対応形式",
                f"このファイル形式は対応していません: {Path(path).suffix}\n\n"
                f"対応形式: {', '.join(sorted(_ACCEPTED_EXTS))}",
            )
            return
        self._file_label.configure(text=f"✓  {Path(path).name}")
        self._on_file(path)


# ─── Main Application ──────────────────────────────────────────────────────────

class App(_AppBase):  # type: ignore[misc]

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.title("Transcription App")
        self.geometry("1020x700")
        self.minsize(800, 560)

        self._engine = TranscriptionEngine()
        self._segments: list[Segment] = []
        self._current_file: Optional[str] = None
        self._queue: queue.Queue = queue.Queue()
        self._running = False
        self._edit_mode = False
        self._card_editors: dict[int, tk.Text] = {}

        # speaker label → display colour (assigned on first appearance)
        self._speaker_colors: dict[str, str] = {}
        # speaker label → editable display name (persisted per file)
        self._speaker_names: dict[str, str] = {}

        cfg = _load_config()
        # HF token: saved config > HF_TOKEN env var > empty
        default_token = cfg.get("hf_token") or os.environ.get("HF_TOKEN", "")
        self._hf_token = tk.StringVar(value=default_token)
        self._model_id = tk.StringVar(value=cfg.get("model", _MODELS[0][1]))
        self._lang_code: Optional[str] = cfg.get("language", None)
        self._use_diarization = tk.BooleanVar(value=cfg.get("diarization", True))

        self._build_ui()
        self._poll()

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=_LEFT_WIDTH)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_left()
        self._build_right()

    def _build_left(self):
        left = ctk.CTkFrame(self, width=_LEFT_WIDTH, corner_radius=0, fg_color=_BG_LEFT)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(2, weight=1)

        # thin right-side separator
        sep = tk.Frame(left, width=1, bg=_BORDER)
        sep.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")

        ctk.CTkLabel(
            left, text="Transcription",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=_TEXT,
        ).grid(row=0, column=0, padx=16, pady=(20, 4), sticky="w")

        self._drop_zone = DropZone(
            left, on_file=self._on_file_selected,
            corner_radius=10, width=_LEFT_WIDTH - 24,
        )
        self._drop_zone.grid(row=1, column=0, padx=12, pady=(4, 8), sticky="ew")

        # ── Settings card ──────────────────────────────────────────────
        card = ctk.CTkFrame(left, corner_radius=10, fg_color=_BG_CARD,
                            border_width=1, border_color=_BORDER)
        card.grid(row=2, column=0, padx=12, pady=4, sticky="new")
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card, text="設定",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=_TEXT_MUTED,
        ).grid(row=0, column=0, padx=14, pady=(12, 6), sticky="w")

        # Model
        ctk.CTkLabel(card, text="モデル",
                     font=ctk.CTkFont(size=11), text_color=_TEXT).grid(
            row=1, column=0, padx=14, pady=(0, 2), sticky="w"
        )
        model_names = [m[0] for m in _MODELS]
        model_ids   = [m[1] for m in _MODELS]
        cur_model = next((i for i, m in enumerate(_MODELS) if m[1] == self._model_id.get()), 0)
        self._model_menu = ctk.CTkOptionMenu(
            card, values=model_names,
            fg_color="#FFFFFF", button_color=_BORDER, button_hover_color=_BORDER,
            text_color=_TEXT, dropdown_text_color=_TEXT,
            command=lambda v: self._model_id.set(model_ids[model_names.index(v)]),
        )
        self._model_menu.set(model_names[cur_model])
        self._model_menu.grid(row=2, column=0, padx=14, pady=(0, 8), sticky="ew")

        # Language
        ctk.CTkLabel(card, text="言語",
                     font=ctk.CTkFont(size=11), text_color=_TEXT).grid(
            row=3, column=0, padx=14, pady=(0, 2), sticky="w"
        )
        lang_names = [l[0] for l in _LANGUAGES]
        lang_codes  = [l[1] for l in _LANGUAGES]
        cur_lang = next((i for i, l in enumerate(_LANGUAGES) if l[1] == self._lang_code), 0)
        self._lang_menu = ctk.CTkOptionMenu(
            card, values=lang_names,
            fg_color="#FFFFFF", button_color=_BORDER, button_hover_color=_BORDER,
            text_color=_TEXT, dropdown_text_color=_TEXT,
            command=lambda v: setattr(self, "_lang_code", lang_codes[lang_names.index(v)]),
        )
        self._lang_menu.set(lang_names[cur_lang])
        self._lang_menu.grid(row=4, column=0, padx=14, pady=(0, 8), sticky="ew")

        # Diarization switch
        self._diar_switch = ctk.CTkSwitch(
            card, text="話者分離を有効にする",
            font=ctk.CTkFont(size=11), text_color=_TEXT,
            progress_color=_ACCENT,
            variable=self._use_diarization,
            command=self._toggle_diarization,
        )
        self._diar_switch.grid(row=5, column=0, padx=14, pady=(0, 8), sticky="w")

        # HF token
        self._hf_frame = ctk.CTkFrame(card, fg_color="transparent")
        self._hf_frame.grid(row=6, column=0, padx=0, pady=(0, 10), sticky="ew")
        self._hf_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self._hf_frame, text="HuggingFace Token",
            font=ctk.CTkFont(size=11), text_color=_TEXT,
        ).grid(row=0, column=0, padx=14, pady=(0, 2), sticky="w")
        ctk.CTkEntry(
            self._hf_frame,
            textvariable=self._hf_token,
            placeholder_text="hf_xxxxxxxxxxxxxxxx",
            fg_color="#FFFFFF", border_color=_BORDER,
            text_color=_TEXT,
            show="•",
        ).grid(row=1, column=0, padx=14, pady=(0, 2), sticky="ew")
        ctk.CTkLabel(
            self._hf_frame,
            text="話者分離モデルの利用に必要",
            font=ctk.CTkFont(size=10),
            text_color=_TEXT_MUTED,
            justify="left",
        ).grid(row=2, column=0, padx=14, sticky="w")

        self._toggle_diarization()

        # Start button
        self._start_btn = ctk.CTkButton(
            left, text="文字起こし開始",
            height=42, corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=_ACCENT, hover_color=_ACCENT_HOV,
            command=self._start,
            state="disabled",
        )
        self._start_btn.grid(row=3, column=0, padx=12, pady=(10, 4), sticky="ew")

        self._progress_label = ctk.CTkLabel(
            left, text="", font=ctk.CTkFont(size=11),
            text_color=_TEXT_MUTED,
        )
        self._progress_label.grid(row=4, column=0, padx=16, sticky="w")

        self._progress_bar = ctk.CTkProgressBar(left, progress_color=_ACCENT, fg_color=_BORDER)
        self._progress_bar.set(0)
        self._progress_bar.grid(row=5, column=0, padx=12, pady=(2, 8), sticky="ew")
        self._progress_bar.grid_remove()

        # Project save / open
        proj_frame = ctk.CTkFrame(left, fg_color="transparent")
        proj_frame.grid(row=6, column=0, padx=12, pady=(0, 14), sticky="ew")
        proj_frame.grid_columnconfigure((0, 1), weight=1)

        _pbtn = dict(
            height=34, corner_radius=8,
            fg_color="#FFFFFF",
            border_width=1, border_color=_BORDER,
            font=ctk.CTkFont(size=11),
        )
        self._save_proj_btn = ctk.CTkButton(
            proj_frame, text="保存",
            text_color=_ACCENT, hover_color="#EFF6FF",
            command=self._save_project, state="disabled",
            **_pbtn,
        )
        self._save_proj_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self._open_proj_btn = ctk.CTkButton(
            proj_frame, text="開く",
            text_color=_TEXT, hover_color=_BG_CARD,
            command=self._load_project,
            **_pbtn,
        )
        self._open_proj_btn.grid(row=0, column=1, padx=(4, 0), sticky="ew")

    def _build_right(self):
        right = ctk.CTkFrame(self, corner_radius=0, fg_color=_BG_RIGHT)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        # Toolbar
        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=0, column=0, padx=16, pady=(16, 6), sticky="ew")

        ctk.CTkLabel(
            bar, text="文字起こし結果",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=_TEXT,
        ).pack(side="left")

        _btn = dict(
            width=68, height=30, corner_radius=8,
            fg_color="#FFFFFF",
            text_color=_ACCENT,
            border_width=1, border_color=_BORDER,
            hover_color="#EFF6FF",
            state="disabled",
        )
        self._json_btn = ctk.CTkButton(bar, text="JSON", command=lambda: self._export("json"), **_btn)
        self._json_btn.pack(side="right", padx=(4, 0))
        self._srt_btn = ctk.CTkButton(bar, text="SRT",  command=lambda: self._export("srt"),  **_btn)
        self._srt_btn.pack(side="right", padx=(4, 0))
        self._txt_btn = ctk.CTkButton(bar, text="TXT",  command=lambda: self._export("txt"),  **_btn)
        self._txt_btn.pack(side="right", padx=(4, 0))

        self._edit_btn = ctk.CTkButton(
            bar, text="編集", width=68, height=30, corner_radius=8,
            fg_color="#FFFFFF", text_color=_TEXT,
            border_width=1, border_color=_BORDER,
            hover_color=_BG_CARD,
            command=self._toggle_edit_mode, state="disabled",
        )
        self._edit_btn.pack(side="right", padx=(4, 14))

        self._copy_btn = ctk.CTkButton(
            bar, text="コピー", width=68, height=30, corner_radius=8,
            fg_color="#FFFFFF", text_color=_TEXT,
            border_width=1, border_color=_BORDER,
            hover_color=_BG_CARD,
            command=self._copy_to_clipboard, state="disabled",
        )
        self._copy_btn.pack(side="right", padx=(4, 0))

        # Card scroll area
        self._cards_container = ctk.CTkScrollableFrame(
            right, corner_radius=10, fg_color="#FFFFFF",
            border_width=1, border_color=_BORDER,
            scrollbar_button_color=_BORDER,
            scrollbar_button_hover_color="#CBD5E1",
        )
        self._cards_container.grid(row=1, column=0, padx=16, pady=(0, 16), sticky="nsew")
        self._cards_container.grid_columnconfigure(0, weight=1)

        self._show_placeholder("ファイルを選択して「文字起こし開始」ボタンを押してください。")

    # ── Handlers ──────────────────────────────────────────────────────

    def _toggle_diarization(self):
        if self._use_diarization.get():
            self._hf_frame.grid()
        else:
            self._hf_frame.grid_remove()

    def _on_file_selected(self, path: str):
        self._current_file = path
        self._start_btn.configure(state="normal")
        self._show_placeholder("「文字起こし開始」ボタンを押すと処理が始まります。")

    def _start(self):
        if self._running or not self._current_file:
            return
        if self._edit_mode:
            self._exit_edit_mode(save=False)

        token = self._hf_token.get().strip() or os.environ.get("HF_TOKEN", "")
        if self._use_diarization.get() and not token:
            messagebox.showwarning(
                "HFトークン未設定",
                "話者分離を使用するには HuggingFace Token が必要です。\n"
                "・テキストボックスに入力するか\n"
                "・ターミナルで export HF_TOKEN=hf_xxx を実行してから起動してください。",
            )
            return

        _save_config({
            "hf_token": self._hf_token.get(),
            "model": self._model_id.get(),
            "language": self._lang_code,
            "diarization": self._use_diarization.get(),
        })

        self._running = True
        self._segments = []
        self._speaker_colors = {}
        self._set_export_state("disabled")
        self._start_btn.configure(state="disabled", text="処理中…")
        self._progress_bar.set(0)
        self._progress_bar.grid()
        self._progress_label.configure(text="開始中…")
        self._clear_text()

        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        def cb(msg: str, pct: int):
            self._queue.put(("progress", msg, pct))

        try:
            token = self._hf_token.get().strip() or None
            segs = self._engine.transcribe(
                file_path=self._current_file,
                model=self._model_id.get(),
                language=self._lang_code,
                use_diarization=self._use_diarization.get(),
                hf_token=token,  # None → engine falls back to HF_TOKEN env var
                progress_callback=cb,
            )
            self._queue.put(("done", segs))
        except Exception as exc:
            self._queue.put(("error", str(exc)))

    def _poll(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self._progress_label.configure(text=msg[1])
                    self._progress_bar.set(msg[2] / 100)
                elif kind == "done":
                    self._on_done(msg[1])
                elif kind == "error":
                    self._on_error(msg[1])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_done(self, segments: list[Segment]):
        self._running = False
        self._segments = segments
        self._start_btn.configure(state="normal", text="文字起こし開始")
        self._progress_bar.set(1.0)
        self._progress_label.configure(text=f"完了  ({len(segments)} ブロック)")
        self._load_speaker_names()
        self._render(segments)
        self._set_export_state("normal")

    def _on_error(self, error: str):
        self._running = False
        self._start_btn.configure(state="normal", text="文字起こし開始")
        self._progress_label.configure(text="エラーが発生しました")
        self._progress_bar.grid_remove()
        messagebox.showerror("エラー", f"文字起こし中にエラーが発生しました:\n\n{error}")

    # ── Results rendering ──────────────────────────────────────────────

    def _speaker_color(self, label: str) -> str:
        if label not in self._speaker_colors:
            idx = len(self._speaker_colors) % len(_SPEAKER_COLORS)
            self._speaker_colors[label] = _SPEAKER_COLORS[idx]
        return self._speaker_colors[label]

    def _known_names(self) -> list[str]:
        names: set[str] = set(self._speaker_names.values())
        for seg in self._segments:
            if seg.display_name:
                names.add(seg.display_name)
        return sorted(names)

    def _clear_text(self):
        for w in self._cards_container.winfo_children():
            w.destroy()
        self._card_editors = {}

    def _show_placeholder(self, text: str):
        self._clear_text()
        ctk.CTkLabel(
            self._cards_container, text=text,
            text_color=_TEXT_MUTED, wraplength=500,
        ).grid(row=0, column=0, padx=20, pady=60)

    def _render(self, segments: list[Segment]):
        self._clear_text()
        if not segments:
            self._show_placeholder("文字起こし結果がありません。")
            return
        for i, seg in enumerate(segments):
            self._make_card(i, seg, edit_mode=False)

    def _make_card(self, idx: int, seg: "Segment", edit_mode: bool):
        label = seg.display_name or self._speaker_names.get(seg.speaker, seg.speaker)
        color = self._speaker_color(label)

        card = ctk.CTkFrame(
            self._cards_container, corner_radius=8,
            fg_color=_BG_CARD, border_width=1, border_color=_BORDER,
        )
        card.grid(row=idx, column=0, padx=10, pady=(6, 0), sticky="ew")
        card.grid_columnconfigure(0, weight=1)

        # Header row
        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=10, pady=(8, 2), sticky="ew")

        dot = tk.Canvas(hdr, width=10, height=10, bg=_BG_CARD, highlightthickness=0)
        dot.create_oval(1, 1, 9, 9, fill=color, outline="")
        dot.pack(side="left", padx=(0, 6), pady=3)

        spk_btn = ctk.CTkButton(
            hdr, text=label,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=color,
            fg_color="transparent", hover_color=_BG_RIGHT,
            width=0, height=22, corner_radius=4, anchor="w",
            command=lambda i=idx: self._on_header_click(i),
        )
        spk_btn.pack(side="left")

        ctk.CTkLabel(
            hdr, text=format_time(seg.start),
            font=ctk.CTkFont(size=10), text_color=_TEXT_MUTED,
        ).pack(side="left", padx=(8, 0))

        # Body text
        body = tk.Text(
            card,
            font=("Helvetica Neue", 13),
            wrap="word",
            relief="flat", bd=0,
            bg=_BG_CARD if not edit_mode else "#FFFFFF",
            fg=_TEXT,
            padx=10, pady=2,
            highlightthickness=0,
            insertbackground=_TEXT,
            selectbackground="#DBEAFE",
            cursor="arrow" if not edit_mode else "xterm",
            height=1,
        )
        body.insert("1.0", seg.text)
        body.configure(state="normal" if edit_mode else "disabled")

        if edit_mode:
            body.bind("<Shift-Return>", lambda e, i=idx, b=body: self._split_segment(i, b))
            self._card_editors[idx] = body

        def _auto_height(e=None, b=body):
            try:
                result = b.count("1.0", "end-1c", "displaylines")
                n = result[0] if result else 1
                b.configure(height=max(2, n + 1))
            except Exception:
                pass

        body.bind("<Configure>", _auto_height)
        body.grid(row=1, column=0, padx=2, pady=(0, 8), sticky="ew")

    # ── Speaker renaming ───────────────────────────────────────────────

    def _on_header_click(self, seg_idx: int):
        if self._running or self._edit_mode or seg_idx >= len(self._segments):
            return
        self._show_rename_dialog(self._segments[seg_idx], seg_idx)

    def _show_rename_dialog(self, seg: Segment, seg_idx: int):
        known = self._known_names()
        dlg_h = 320 if known else 250

        dlg = ctk.CTkToplevel(self)
        dlg.title("話者名を変更")
        dlg.geometry(f"420x{dlg_h}")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.lift()
        dlg.focus_force()

        current = seg.display_name if seg.display_name else self._speaker_names.get(seg.speaker, "")

        ctk.CTkLabel(
            dlg,
            text=f"「{seg.speaker}」の表示名を変更",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(padx=20, pady=(20, 6), anchor="w")

        entry = ctk.CTkEntry(dlg, width=380, placeholder_text="新しい名前を入力…")
        entry.pack(padx=20, pady=(0, 4))
        if current:
            entry.insert(0, current)
        entry.focus()

        if known:
            ctk.CTkLabel(
                dlg,
                text="登録済みの名前から選択:",
                font=ctk.CTkFont(size=11),
                text_color=("gray40", "gray60"),
            ).pack(padx=20, pady=(6, 2), anchor="w")

            chips_frame = ctk.CTkFrame(dlg, fg_color="transparent")
            chips_frame.pack(padx=20, anchor="w")

            def _fill(name: str):
                entry.delete(0, "end")
                entry.insert(0, name)
                entry.focus()

            for name in known:
                color = self._speaker_colors.get(name, "#4FC3F7")
                ctk.CTkButton(
                    chips_frame,
                    text=name,
                    width=0, height=28, corner_radius=14,
                    fg_color=color, hover_color=color,
                    text_color="#1a1a1a",
                    font=ctk.CTkFont(size=12),
                    command=lambda n=name: _fill(n),
                ).pack(side="left", padx=(0, 6), pady=2)

        ctk.CTkLabel(
            dlg,
            text="クリックで話者ラベルをすべて更新、または1箇所だけ変更できます。",
            font=ctk.CTkFont(size=10),
            text_color=("gray50", "gray60"),
            wraplength=380,
            justify="left",
        ).pack(padx=20, pady=(8, 0), anchor="w")

        def apply_this():
            name = entry.get().strip()
            if not name:
                return
            seg.display_name = name
            dlg.destroy()
            self._render(self._segments)

        def apply_all():
            name = entry.get().strip()
            if not name:
                return
            self._speaker_names[seg.speaker] = name
            for s in self._segments:
                if s.speaker == seg.speaker:
                    s.display_name = ""
            dlg.destroy()
            self._render(self._segments)
            self._save_speaker_names()

        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack(padx=20, pady=(12, 0), fill="x")

        ctk.CTkButton(
            btn_frame, text="このセグメントのみ",
            width=160, command=apply_this,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame,
            text=f"全ての {seg.speaker} を変更",
            width=190, command=apply_all,
        ).pack(side="left")

        ctk.CTkButton(
            dlg, text="キャンセル",
            fg_color="transparent",
            text_color=("gray40", "gray60"),
            hover_color=("gray85", "gray25"),
            command=dlg.destroy,
        ).pack(pady=(8, 0))

        entry.bind("<Return>", lambda e: apply_all())

    def _save_speaker_names(self):
        if not self._current_file or not self._speaker_names:
            return
        cfg = _load_config()
        all_names = cfg.get("speaker_names", {})
        all_names[self._current_file] = self._speaker_names
        cfg["speaker_names"] = all_names
        _save_config(cfg)

    def _load_speaker_names(self):
        if not self._current_file:
            return
        cfg = _load_config()
        saved = cfg.get("speaker_names", {}).get(self._current_file, {})
        self._speaker_names = dict(saved)

    # ── Edit mode ──────────────────────────────────────────────────────

    def _toggle_edit_mode(self):
        if self._edit_mode:
            self._exit_edit_mode(save=True)
        else:
            self._enter_edit_mode()

    def _enter_edit_mode(self):
        self._edit_mode = True
        self._render_edit_mode()
        self._edit_btn.configure(
            text="完了", fg_color=_ACCENT, text_color="#FFFFFF",
            hover_color=_ACCENT_HOV, border_width=0,
        )
        for btn in (self._txt_btn, self._srt_btn, self._json_btn, self._save_proj_btn):
            btn.configure(state="disabled")

    def _render_edit_mode(self):
        self._clear_text()
        for i, seg in enumerate(self._segments):
            self._make_card(i, seg, edit_mode=True)

    def _exit_edit_mode(self, save: bool = True):
        if save:
            self._sync_edits_to_segments()
        self._edit_mode = False
        self._edit_btn.configure(
            text="編集", fg_color="#FFFFFF", text_color=_TEXT,
            hover_color=_BG_CARD, border_width=1,
        )
        if save and self._segments:
            self._render(self._segments)
        for btn in (self._txt_btn, self._srt_btn, self._json_btn, self._save_proj_btn):
            btn.configure(state="normal")

    def _sync_edits_to_segments(self):
        for idx, body in self._card_editors.items():
            if idx < len(self._segments):
                self._segments[idx].text = body.get("1.0", "end").strip()

    def _split_segment(self, idx: int, body: tk.Text):
        cursor = body.index("insert")
        before = body.get("1.0", cursor).rstrip("\n")
        after = body.get(cursor, "end").strip()
        if not before.strip() or not after:
            return "break"

        self._segments[idx].text = before

        seg = self._segments[idx]
        new_seg = Segment(
            start=seg.start,
            end=seg.end,
            speaker=seg.speaker,
            text=after,
            display_name=seg.display_name,
        )
        self._segments.insert(idx + 1, new_seg)

        self._render_edit_mode()

        if idx + 1 in self._card_editors:
            new_body = self._card_editors[idx + 1]
            new_body.focus_set()
            new_body.mark_set("insert", "1.0")

        return "break"

    # ── Copy ───────────────────────────────────────────────────────────

    def _copy_to_clipboard(self):
        if not self._segments:
            return
        self.clipboard_clear()
        self.clipboard_append(segments_to_txt(self._segments))
        self._copy_btn.configure(text="✓ 完了")
        self.after(2000, lambda: self._copy_btn.configure(text="コピー"))

    # ── Project save / load ────────────────────────────────────────────

    def _save_project(self):
        if not self._segments:
            return
        stem = Path(self._current_file).stem if self._current_file else "project"
        path = filedialog.asksaveasfilename(
            defaultextension=".transcription",
            initialfile=f"{stem}.transcription",
            filetypes=[
                ("Transcription Project", "*.transcription"),
                ("JSON", "*.json"),
                ("すべてのファイル", "*.*"),
            ],
        )
        if not path:
            return
        project = {
            "version": 1,
            "audio_file": self._current_file or "",
            "speaker_names": self._speaker_names,
            "segments": [
                {
                    "start": seg.start,
                    "end": seg.end,
                    "speaker": seg.speaker,
                    "display_name": seg.display_name,
                    "text": seg.text,
                }
                for seg in self._segments
            ],
        }
        Path(path).write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        messagebox.showinfo("保存完了", f"プロジェクトを保存しました:\n{path}")

    def _load_project(self):
        path = filedialog.askopenfilename(
            filetypes=[
                ("Transcription Project", "*.transcription"),
                ("JSON", "*.json"),
                ("すべてのファイル", "*.*"),
            ],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("エラー", f"プロジェクトを読み込めませんでした:\n{exc}")
            return

        self._current_file = data.get("audio_file") or None
        self._speaker_names = data.get("speaker_names", {})
        self._speaker_colors = {}
        self._segments = [
            Segment(
                start=s["start"],
                end=s["end"],
                speaker=s["speaker"],
                text=s["text"],
                display_name=s.get("display_name", ""),
            )
            for s in data.get("segments", [])
        ]
        if self._edit_mode:
            self._edit_mode = False
            self._edit_btn.configure(
                text="編集", fg_color="#FFFFFF", text_color=_TEXT,
                hover_color=_BG_CARD, border_width=1,
            )
        if self._segments:
            self._render(self._segments)
            self._set_export_state("normal")
            self._start_btn.configure(state="normal" if self._current_file else "disabled")
            self._progress_label.configure(
                text=f"プロジェクト読み込み完了  ({len(self._segments)} ブロック)"
            )
        else:
            self._show_placeholder("セグメントが見つかりませんでした。")

    # ── Export ─────────────────────────────────────────────────────────

    def _set_export_state(self, state: str):
        for btn in (self._txt_btn, self._srt_btn, self._json_btn,
                    self._copy_btn, self._edit_btn, self._save_proj_btn):
            btn.configure(state=state)

    def _export(self, fmt: str):
        if not self._segments:
            return
        stem = Path(self._current_file).stem if self._current_file else "transcript"
        ext_map = {"txt": ".txt", "srt": ".srt", "json": ".json"}
        path = filedialog.asksaveasfilename(
            defaultextension=ext_map[fmt],
            initialfile=f"{stem}{ext_map[fmt]}",
            filetypes=[(fmt.upper(), f"*{ext_map[fmt]}"), ("すべてのファイル", "*.*")],
        )
        if not path:
            return
        converters = {"txt": segments_to_txt, "srt": segments_to_srt, "json": segments_to_json}
        Path(path).write_text(converters[fmt](self._segments), encoding="utf-8")
        messagebox.showinfo("保存完了", f"保存しました:\n{path}")


# ─── Entry point ───────────────────────────────────────────────────────────────

def main():
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
