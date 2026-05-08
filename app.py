#!/usr/bin/env python3
import json, os, queue, re, threading, tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional
import customtkinter as ctk
from transcribe_core import Segment, TranscriptionEngine, format_time, segments_to_json, segments_to_srt, segments_to_txt
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True; _AppBase = TkinterDnD.Tk
except ImportError:
    _HAS_DND = False; _AppBase = ctk.CTk
_CONFIG_PATH = Path.home() / ".transcription_app" / "config.json"
_MODELS = [("large-v3  (\u9ad8\u7cbe\u5ea6\u30fb\u6a19\u6e96)", "mlx-community/whisper-large-v3-mlx"), ("large-v3-turbo  (\u9ad8\u901f)", "mlx-community/whisper-large-v3-turbo"), ("medium", "mlx-community/whisper-medium-mlx"), ("small", "mlx-community/whisper-small-mlx"), ("base", "mlx-community/whisper-base-mlx"), ("tiny", "mlx-community/whisper-tiny-mlx")]
_LANGUAGES = [("\u81ea\u52d5\u691c\u51fa", None), ("\u65e5\u672c\u8a9e", "ja"), ("English", "en"), ("\u4e2d\u6587", "zh"), ("\ud55c\uad6d\uc5b4", "ko"), ("Fran\u00e7ais", "fr"), ("Deutsch", "de"), ("Espa\u00f1ol", "es")]
_ACCEPTED_EXTS = {".mp3",".mp4",".m4a",".wav",".aiff",".aac",".ogg",".flac",".mov",".avi",".mkv",".webm",".wma"}
_SPEAKER_COLORS = ["#3B82F6","#10B981","#F59E0B","#EF4444","#8B5CF6","#06B6D4","#F97316","#EC4899","#6366F1","#14B8A6","#84CC16","#A855F7","#0EA5E9","#D946EF","#78716C"]
_BG_LEFT="#FFFFFF"; _BG_RIGHT="#F1F5F9"; _BG_CARD="#F8FAFC"; _ACCENT="#3B82F6"; _ACCENT_HOV="#2563EB"; _BORDER="#E2E8F0"; _TEXT="#1E293B"; _TEXT_MUTED="#94A3B8"; _LEFT_WIDTH=300
def _load_config():
    if _CONFIG_PATH.exists():
        try: return json.loads(_CONFIG_PATH.read_text())
        except: pass
    return {}
def _save_config(cfg):
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
def _make_upload_icon(parent, size=52):
    c = tk.Canvas(parent, width=size, height=size, bg=_BG_LEFT, highlightthickness=0)
    p,col,w = 5,_ACCENT,2
    c.create_oval(p,p,size-p,size-p,outline=col,width=w); cx,cy=size//2,size//2
    c.create_line(cx,cy+10,cx,cy-7,fill=col,width=w,capstyle="round")
    c.create_line(cx-7,cy-1,cx,cy-8,fill=col,width=w,capstyle="round")
    c.create_line(cx+7,cy-1,cx,cy-8,fill=col,width=w,capstyle="round")
    c.create_line(cx-8,cy+10,cx+8,cy+10,fill=col,width=w,capstyle="round")
    return c
class DropZone(ctk.CTkFrame):
    def __init__(self,master,on_file,**kw):
        kw.setdefault("fg_color",_BG_LEFT); super().__init__(master,**kw)
        self._on_file=on_file; self._set_border_normal()
        _make_upload_icon(self,52).pack(pady=(18,4))
        ctk.CTkLabel(self,text="\u3053\u3053\u306b\u30d5\u30a1\u30a4\u30eb\u3092\u30c9\u30ed\u30c3\u30d7" if _HAS_DND else "\u30d5\u30a1\u30a4\u30eb\u3092\u9078\u629e\u3057\u3066\u304f\u3060\u3055\u3044",font=ctk.CTkFont(size=14,weight="bold"),text_color=_TEXT).pack()
        if _HAS_DND: ctk.CTkLabel(self,text="\u307e\u305f\u306f",font=ctk.CTkFont(size=11),text_color=_TEXT_MUTED).pack(pady=1)
        ctk.CTkButton(self,text="\u30d5\u30a1\u30a4\u30eb\u3092\u9078\u629e",width=130,height=32,corner_radius=8,fg_color=_ACCENT,hover_color=_ACCENT_HOV,font=ctk.CTkFont(size=12),command=self._browse).pack(pady=4)
        ctk.CTkLabel(self,text="MP3 \u00b7 MP4 \u00b7 WAV \u00b7 M4A \u00b7 MOV \u00b7 \u305d\u306e\u4ed6",font=ctk.CTkFont(size=10),text_color=_TEXT_MUTED).pack(pady=(2,10))
        self._file_label=ctk.CTkLabel(self,text="",font=ctk.CTkFont(size=11),text_color=_TEXT,wraplength=260); self._file_label.pack(pady=(0,8))
        if _HAS_DND:
            self.drop_target_register(DND_FILES); self.dnd_bind("<<Drop>>",self._on_drop)
            self.dnd_bind("<<DragEnter>>",lambda _e:self._set_border_hover()); self.dnd_bind("<<DragLeave>>",lambda _e:self._set_border_normal())
    def _set_border_normal(self): self.configure(border_width=1,border_color=_BORDER)
    def _set_border_hover(self): self.configure(border_width=2,border_color=_ACCENT)
    def _browse(self):
        exts=" ".join(f"*{e}" for e in sorted(_ACCEPTED_EXTS))
        p=filedialog.askopenfilename(filetypes=[("\u97f3\u58f0\u30fb\u52d5\u753b\u30d5\u30a1\u30a4\u30eb",exts),("\u3059\u3079\u3066\u306e\u30d5\u30a1\u30a4\u30eb","*.*")])
        if p: self._accept(p)
    def _on_drop(self,event): self._set_border_normal(); self._accept(event.data.strip().strip("{}"))
    def _accept(self,path):
        if Path(path).suffix.lower() not in _ACCEPTED_EXTS:
            messagebox.showwarning("\u975e\u5bfe\u5fdc\u5f62\u5f0f",f"\u5bfe\u5fdc\u5916: {Path(path).suffix}"); return
        self._file_label.configure(text=f"\u2713  {Path(path).name}"); self._on_file(path)

class App(_AppBase):
    def __init__(self):
        super().__init__(); ctk.set_appearance_mode("light"); ctk.set_default_color_theme("blue")
        self.title("Transcription App"); self.geometry("1020x700"); self.minsize(800,560)
        self._engine=TranscriptionEngine(); self._segments=[]; self._current_file=None
        self._queue=queue.Queue(); self._running=False; self._edit_mode=False
        self._speaker_colors={}; self._speaker_names={}
        cfg=_load_config()
        self._hf_token=tk.StringVar(value=cfg.get("hf_token") or os.environ.get("HF_TOKEN",""))
        self._model_id=tk.StringVar(value=cfg.get("model",_MODELS[0][1]))
        self._lang_code=cfg.get("language",None)
        self._use_diarization=tk.BooleanVar(value=cfg.get("diarization",True))
        self._build_ui(); self._poll()
    def _build_ui(self):
        self.grid_columnconfigure(0,weight=0,minsize=_LEFT_WIDTH); self.grid_columnconfigure(1,weight=1); self.grid_rowconfigure(0,weight=1)
        self._build_left(); self._build_right()
    def _build_left(self):
        left=ctk.CTkFrame(self,width=_LEFT_WIDTH,corner_radius=0,fg_color=_BG_LEFT)
        left.grid(row=0,column=0,sticky="nsew"); left.grid_propagate(False)
        left.grid_columnconfigure(0,weight=1); left.grid_rowconfigure(2,weight=1)
        tk.Frame(left,width=1,bg=_BORDER).place(relx=1.0,rely=0,relheight=1.0,anchor="ne")
        ctk.CTkLabel(left,text="Transcription",font=ctk.CTkFont(size=18,weight="bold"),text_color=_TEXT).grid(row=0,column=0,padx=16,pady=(20,4),sticky="w")
        self._drop_zone=DropZone(left,on_file=self._on_file_selected,corner_radius=10,width=_LEFT_WIDTH-24)
        self._drop_zone.grid(row=1,column=0,padx=12,pady=(4,8),sticky="ew")
        card=ctk.CTkFrame(left,corner_radius=10,fg_color=_BG_CARD,border_width=1,border_color=_BORDER)
        card.grid(row=2,column=0,padx=12,pady=4,sticky="new"); card.grid_columnconfigure(0,weight=1)
        ctk.CTkLabel(card,text="\u8a2d\u5b9a",font=ctk.CTkFont(size=12,weight="bold"),text_color=_TEXT_MUTED).grid(row=0,column=0,padx=14,pady=(12,6),sticky="w")
        ctk.CTkLabel(card,text="\u30e2\u30c7\u30eb",font=ctk.CTkFont(size=11),text_color=_TEXT).grid(row=1,column=0,padx=14,pady=(0,2),sticky="w")
        mn=[m[0] for m in _MODELS]; mi=[m[1] for m in _MODELS]; cm=next((i for i,m in enumerate(_MODELS) if m[1]==self._model_id.get()),0)
        self._model_menu=ctk.CTkOptionMenu(card,values=mn,fg_color="#FFFFFF",button_color=_BORDER,button_hover_color=_BORDER,text_color=_TEXT,dropdown_text_color=_TEXT,command=lambda v:self._model_id.set(mi[mn.index(v)]))
        self._model_menu.set(mn[cm]); self._model_menu.grid(row=2,column=0,padx=14,pady=(0,8),sticky="ew")
        ctk.CTkLabel(card,text="\u8a00\u8a9e",font=ctk.CTkFont(size=11),text_color=_TEXT).grid(row=3,column=0,padx=14,pady=(0,2),sticky="w")
        ln=[l[0] for l in _LANGUAGES]; lc=[l[1] for l in _LANGUAGES]; cl=next((i for i,l in enumerate(_LANGUAGES) if l[1]==self._lang_code),0)
        self._lang_menu=ctk.CTkOptionMenu(card,values=ln,fg_color="#FFFFFF",button_color=_BORDER,button_hover_color=_BORDER,text_color=_TEXT,dropdown_text_color=_TEXT,command=lambda v:setattr(self,"_lang_code",lc[ln.index(v)]))
        self._lang_menu.set(ln[cl]); self._lang_menu.grid(row=4,column=0,padx=14,pady=(0,8),sticky="ew")
        self._diar_switch=ctk.CTkSwitch(card,text="\u8a71\u8005\u5206\u96e2\u3092\u6709\u52b9\u306b\u3059\u308b",font=ctk.CTkFont(size=11),text_color=_TEXT,progress_color=_ACCENT,variable=self._use_diarization,command=self._toggle_diarization)
        self._diar_switch.grid(row=5,column=0,padx=14,pady=(0,8),sticky="w")
        self._hf_frame=ctk.CTkFrame(card,fg_color="transparent"); self._hf_frame.grid(row=6,column=0,padx=0,pady=(0,10),sticky="ew"); self._hf_frame.grid_columnconfigure(0,weight=1)
        ctk.CTkLabel(self._hf_frame,text="HuggingFace Token",font=ctk.CTkFont(size=11),text_color=_TEXT).grid(row=0,column=0,padx=14,pady=(0,2),sticky="w")
        ctk.CTkEntry(self._hf_frame,textvariable=self._hf_token,placeholder_text="hf_xxxxxxxxxxxxxxxx",fg_color="#FFFFFF",border_color=_BORDER,text_color=_TEXT,show="\u2022").grid(row=1,column=0,padx=14,pady=(0,2),sticky="ew")
        ctk.CTkLabel(self._hf_frame,text="\u8a71\u8005\u5206\u96e2\u30e2\u30c7\u30eb\u306e\u5229\u7528\u306b\u5fc5\u8981",font=ctk.CTkFont(size=10),text_color=_TEXT_MUTED,justify="left").grid(row=2,column=0,padx=14,sticky="w")
        self._toggle_diarization()
        self._start_btn=ctk.CTkButton(left,text="\u6587\u5b57\u8d77\u3053\u3057\u958b\u59cb",height=42,corner_radius=10,font=ctk.CTkFont(size=13,weight="bold"),fg_color=_ACCENT,hover_color=_ACCENT_HOV,command=self._start,state="disabled")
        self._start_btn.grid(row=3,column=0,padx=12,pady=(10,4),sticky="ew")
        self._progress_label=ctk.CTkLabel(left,text="",font=ctk.CTkFont(size=11),text_color=_TEXT_MUTED); self._progress_label.grid(row=4,column=0,padx=16,sticky="w")
        self._progress_bar=ctk.CTkProgressBar(left,progress_color=_ACCENT,fg_color=_BORDER); self._progress_bar.set(0)
        self._progress_bar.grid(row=5,column=0,padx=12,pady=(2,8),sticky="ew"); self._progress_bar.grid_remove()
        pf=ctk.CTkFrame(left,fg_color="transparent"); pf.grid(row=6,column=0,padx=12,pady=(0,14),sticky="ew"); pf.grid_columnconfigure((0,1),weight=1)
        pb=dict(height=34,corner_radius=8,fg_color="#FFFFFF",border_width=1,border_color=_BORDER,font=ctk.CTkFont(size=11))
        self._save_proj_btn=ctk.CTkButton(pf,text="\u4fdd\u5b58",text_color=_ACCENT,hover_color="#EFF6FF",command=self._save_project,state="disabled",**pb)
        self._save_proj_btn.grid(row=0,column=0,padx=(0,4),sticky="ew")
        self._open_proj_btn=ctk.CTkButton(pf,text="\u958b\u304f",text_color=_TEXT,hover_color=_BG_CARD,command=self._load_project,**pb)
        self._open_proj_btn.grid(row=0,column=1,padx=(4,0),sticky="ew")
    def _build_right(self):
        right=ctk.CTkFrame(self,corner_radius=0,fg_color=_BG_RIGHT); right.grid(row=0,column=1,sticky="nsew")
        right.grid_columnconfigure(0,weight=1); right.grid_rowconfigure(1,weight=1)
        bar=ctk.CTkFrame(right,fg_color="transparent"); bar.grid(row=0,column=0,padx=16,pady=(16,6),sticky="ew")
        ctk.CTkLabel(bar,text="\u6587\u5b57\u8d77\u3053\u3057\u7d50\u679c",font=ctk.CTkFont(size=15,weight="bold"),text_color=_TEXT).pack(side="left")
        eb=dict(width=68,height=30,corner_radius=8,fg_color="#FFFFFF",text_color=_ACCENT,border_width=1,border_color=_BORDER,hover_color="#EFF6FF",state="disabled")
        self._json_btn=ctk.CTkButton(bar,text="JSON",command=lambda:self._export("json"),**eb); self._json_btn.pack(side="right",padx=(4,0))
        self._srt_btn=ctk.CTkButton(bar,text="SRT",command=lambda:self._export("srt"),**eb); self._srt_btn.pack(side="right",padx=(4,0))
        self._txt_btn=ctk.CTkButton(bar,text="TXT",command=lambda:self._export("txt"),**eb); self._txt_btn.pack(side="right",padx=(4,0))
        self._edit_btn=ctk.CTkButton(bar,text="\u7de8\u96c6",width=68,height=30,corner_radius=8,fg_color="#FFFFFF",text_color=_TEXT,border_width=1,border_color=_BORDER,hover_color=_BG_CARD,command=self._toggle_edit_mode,state="disabled")
        self._edit_btn.pack(side="right",padx=(4,14))
        self._copy_btn=ctk.CTkButton(bar,text="\u30b3\u30d4\u30fc",width=68,height=30,corner_radius=8,fg_color="#FFFFFF",text_color=_TEXT,border_width=1,border_color=_BORDER,hover_color=_BG_CARD,command=self._copy_to_clipboard,state="disabled")
        self._copy_btn.pack(side="right",padx=(4,0))
        outer=ctk.CTkFrame(right,corner_radius=10,fg_color="#FFFFFF",border_width=1,border_color=_BORDER)
        outer.grid(row=1,column=0,padx=16,pady=(0,16),sticky="nsew"); outer.grid_columnconfigure(0,weight=1); outer.grid_rowconfigure(0,weight=1)
        self._textbox=ctk.CTkTextbox(outer,font=ctk.CTkFont(family="Helvetica Neue",size=14),wrap="word",state="disabled",corner_radius=10,fg_color="#FFFFFF",text_color=_TEXT,scrollbar_button_color=_BORDER,scrollbar_button_hover_color="#CBD5E1")
        self._textbox.grid(row=0,column=0,sticky="nsew"); self._inner=self._textbox._textbox
        self._show_placeholder("\u30d5\u30a1\u30a4\u30eb\u3092\u9078\u629e\u3057\u3066\u300c\u6587\u5b57\u8d77\u3053\u3057\u958b\u59cb\u300d\u30dc\u30bf\u30f3\u3092\u62bc\u3057\u3066\u304f\u3060\u3055\u3044\u3002")
    def _toggle_diarization(self):
        if self._use_diarization.get(): self._hf_frame.grid()
        else: self._hf_frame.grid_remove()
    def _on_file_selected(self,path):
        self._current_file=path; self._start_btn.configure(state="normal")
        self._show_placeholder("\u300c\u6587\u5b57\u8d77\u3053\u3057\u958b\u59cb\u300d\u30dc\u30bf\u30f3\u3092\u62bc\u3059\u3068\u51e6\u7406\u304c\u59cb\u307e\u308a\u307e\u3059\u3002")
    def _start(self):
        if self._running or not self._current_file: return
        if self._edit_mode: self._exit_edit_mode(save=False)
        token=self._hf_token.get().strip() or os.environ.get("HF_TOKEN","")
        if self._use_diarization.get() and not token:
            messagebox.showwarning("HF\u30c8\u30fc\u30af\u30f3\u672a\u8a2d\u5b9a","\u8a71\u8005\u5206\u96e2\u3092\u4f7f\u7528\u3059\u308b\u306b\u306f HuggingFace Token \u304c\u5fc5\u8981\u3067\u3059\u3002"); return
        _save_config({"hf_token":self._hf_token.get(),"model":self._model_id.get(),"language":self._lang_code,"diarization":self._use_diarization.get()})
        self._running=True; self._segments=[]; self._speaker_colors={}
        self._set_export_state("disabled"); self._start_btn.configure(state="disabled",text="\u51e6\u7406\u4e2d\u2026")
        self._progress_bar.set(0); self._progress_bar.grid(); self._progress_label.configure(text="\u958b\u59cb\u4e2d\u2026"); self._clear_text()
        threading.Thread(target=self._worker,daemon=True).start()
    def _worker(self):
        def cb(msg,pct): self._queue.put(("progress",msg,pct))
        try:
            segs=self._engine.transcribe(file_path=self._current_file,model=self._model_id.get(),language=self._lang_code,use_diarization=self._use_diarization.get(),hf_token=self._hf_token.get().strip() or None,progress_callback=cb)
            self._queue.put(("done",segs))
        except Exception as exc: self._queue.put(("error",str(exc)))
    def _poll(self):
        try:
            while True:
                msg=self._queue.get_nowait(); k=msg[0]
                if k=="progress": self._progress_label.configure(text=msg[1]); self._progress_bar.set(msg[2]/100)
                elif k=="done": self._on_done(msg[1])
                elif k=="error": self._on_error(msg[1])
        except queue.Empty: pass
        self.after(100,self._poll)
    def _on_done(self,segments):
        self._running=False; self._segments=segments
        self._start_btn.configure(state="normal",text="\u6587\u5b57\u8d77\u3053\u3057\u958b\u59cb")
        self._progress_bar.set(1.0); self._progress_label.configure(text=f"\u5b8c\u4e86  ({len(segments)} \u30d6\u30ed\u30c3\u30af)")
        self._load_speaker_names(); self._render(segments); self._set_export_state("normal")
    def _on_error(self,error):
        self._running=False; self._start_btn.configure(state="normal",text="\u6587\u5b57\u8d77\u3053\u3057\u958b\u59cb")
        self._progress_label.configure(text="\u30a8\u30e9\u30fc\u304c\u767a\u751f\u3057\u307e\u3057\u305f"); self._progress_bar.grid_remove()
        messagebox.showerror("\u30a8\u30e9\u30fc",f"\u30a8\u30e9\u30fc\u304c\u767a\u751f\u3057\u307e\u3057\u305f:\n\n{error}")
    def _speaker_color(self,label):
        if label not in self._speaker_colors:
            self._speaker_colors[label]=_SPEAKER_COLORS[len(self._speaker_colors)%len(_SPEAKER_COLORS)]
        return self._speaker_colors[label]
    def _known_names(self):
        names=set(self._speaker_names.values())
        for seg in self._segments:
            if seg.display_name: names.add(seg.display_name)
        return sorted(names)
    def _clear_text(self):
        self._textbox.configure(state="normal"); self._textbox.delete("0.0","end"); self._textbox.configure(state="disabled")
    def _show_placeholder(self,text):
        self._textbox.configure(state="normal"); self._textbox.delete("0.0","end")
        self._inner.tag_config("placeholder",foreground="#888888"); self._inner.insert("end",text,"placeholder"); self._textbox.configure(state="disabled")
    def _render(self,segments):
        self._textbox.configure(state="normal"); self._textbox.delete("0.0","end")
        if not segments: self._show_placeholder("\u6587\u5b57\u8d77\u3053\u3057\u7d50\u679c\u304c\u3042\u308a\u307e\u305b\u3093\u3002"); return
        for i,seg in enumerate(segments):
            label=seg.display_name if seg.display_name else self._speaker_names.get(seg.speaker,seg.speaker)
            color=self._speaker_color(label); hdr_tag=f"hdr_{i}"
            self._inner.tag_config(hdr_tag,foreground=color,font=("Helvetica Neue",13,"bold"))
            self._inner.tag_bind(hdr_tag,"<Button-1>",lambda e,idx=i:self._on_header_click(idx))
            self._inner.tag_bind(hdr_tag,"<Enter>",lambda e:self._inner.config(cursor="hand2"))
            self._inner.tag_bind(hdr_tag,"<Leave>",lambda e:self._inner.config(cursor=""))
            if i>0: self._inner.insert("end","\n")
            self._inner.insert("end",f"[{format_time(seg.start)} {label}]  \u270e\n",hdr_tag)
            self._inner.insert("end",seg.text+"\n")
        self._textbox.configure(state="disabled")
    def _on_header_click(self,seg_idx):
        if self._running or self._edit_mode or seg_idx>=len(self._segments): return
        self._show_rename_dialog(self._segments[seg_idx],seg_idx)
    def _show_rename_dialog(self,seg,seg_idx):
        known=self._known_names(); dlg=ctk.CTkToplevel(self)
        dlg.title("\u8a71\u8005\u540d\u3092\u5909\u66f4"); dlg.geometry(f"420x{'320' if known else '250'}"); dlg.resizable(False,False)
        dlg.grab_set(); dlg.lift(); dlg.focus_force()
        current=seg.display_name if seg.display_name else self._speaker_names.get(seg.speaker,"")
        ctk.CTkLabel(dlg,text=f"\u300c{seg.speaker}\u300d\u306e\u8868\u793a\u540d\u3092\u5909\u66f4",font=ctk.CTkFont(size=13,weight="bold")).pack(padx=20,pady=(20,6),anchor="w")
        entry=ctk.CTkEntry(dlg,width=380,placeholder_text="\u65b0\u3057\u3044\u540d\u524d\u3092\u5165\u529b\u2026"); entry.pack(padx=20,pady=(0,4))
        if current: entry.insert(0,current)
        entry.focus()
        if known:
            ctk.CTkLabel(dlg,text="\u767b\u9332\u6e08\u307f\u306e\u540d\u524d\u304b\u3089\u9078\u629e:",font=ctk.CTkFont(size=11),text_color=("gray40","gray60")).pack(padx=20,pady=(6,2),anchor="w")
            cf=ctk.CTkFrame(dlg,fg_color="transparent"); cf.pack(padx=20,anchor="w")
            def _fill(name): entry.delete(0,"end"); entry.insert(0,name); entry.focus()
            for name in known:
                c=self._speaker_colors.get(name,"#4FC3F7")
                ctk.CTkButton(cf,text=name,width=0,height=28,corner_radius=14,fg_color=c,hover_color=c,text_color="#1a1a1a",font=ctk.CTkFont(size=12),command=lambda n=name:_fill(n)).pack(side="left",padx=(0,6),pady=2)
        ctk.CTkLabel(dlg,text="\u30af\u30ea\u30c3\u30af\u3067\u8a71\u8005\u30e9\u30d9\u30eb\u3092\u3059\u3079\u3066\u66f4\u65b0\u3001\u307e\u305f\u306f1\u7b87\u6240\u3060\u3051\u5909\u66f4\u3067\u304d\u307e\u3059\u3002",font=ctk.CTkFont(size=10),text_color=("gray50","gray60"),wraplength=380,justify="left").pack(padx=20,pady=(8,0),anchor="w")
        def apply_this():
            n=entry.get().strip()
            if not n: return
            seg.display_name=n; dlg.destroy(); self._render(self._segments)
        def apply_all():
            n=entry.get().strip()
            if not n: return
            self._speaker_names[seg.speaker]=n
            for s in self._segments:
                if s.speaker==seg.speaker: s.display_name=""
            dlg.destroy(); self._render(self._segments); self._save_speaker_names()
        bf=ctk.CTkFrame(dlg,fg_color="transparent"); bf.pack(padx=20,pady=(12,0),fill="x")
        ctk.CTkButton(bf,text="\u3053\u306e\u30bb\u30b0\u30e1\u30f3\u30c8\u306e\u307f",width=160,command=apply_this).pack(side="left",padx=(0,8))
        ctk.CTkButton(bf,text=f"\u5168\u3066\u306e {seg.speaker} \u3092\u5909\u66f4",width=190,command=apply_all).pack(side="left")
        ctk.CTkButton(dlg,text="\u30ad\u30e3\u30f3\u30bb\u30eb",fg_color="transparent",text_color=("gray40","gray60"),hover_color=("gray85","gray25"),command=dlg.destroy).pack(pady=(8,0))
        entry.bind("<Return>",lambda e:apply_all())
    def _save_speaker_names(self):
        if not self._current_file or not self._speaker_names: return
        cfg=_load_config(); sn=cfg.get("speaker_names",{}); sn[self._current_file]=self._speaker_names; cfg["speaker_names"]=sn; _save_config(cfg)
    def _load_speaker_names(self):
        if not self._current_file: return
        cfg=_load_config(); self._speaker_names=dict(cfg.get("speaker_names",{}).get(self._current_file,{}))
    def _toggle_edit_mode(self):
        if self._edit_mode: self._exit_edit_mode(save=True)
        else: self._enter_edit_mode()

    def _enter_edit_mode(self):
        self._edit_mode = True
        self._render_edit_mode()
        self._edit_btn.configure(text='完了', fg_color=_ACCENT, text_color='#FFFFFF', hover_color=_ACCENT_HOV, border_width=0)
        for b in (self._txt_btn, self._srt_btn, self._json_btn, self._save_proj_btn): b.configure(state='disabled')
        self._gbid_key   = self._inner.bind('<Key>',      self._guard_header, add=True)
        self._gbid_del   = self._inner.bind('<Delete>',   self._guard_header, add=True)
        self._gbid_paste = self._inner.bind('<<Paste>>',  self._guard_header, add=True)
        self._gbid_cut   = self._inner.bind('<<Cut>>',    self._guard_header, add=True)

    def _render_edit_mode(self):
        self._textbox.configure(state='normal')
        self._textbox.delete('0.0', 'end')
        if not self._segments: return
        for i, seg in enumerate(self._segments):
            label = seg.display_name if seg.display_name else self._speaker_names.get(seg.speaker, seg.speaker)
            color = self._speaker_color(label)
            hdr_tag = f'hdr_{i}'
            self._inner.tag_config(hdr_tag, foreground=color, font=('Helvetica Neue', 13, 'bold'))
            if i > 0: self._inner.insert('end', '\n')
            self._inner.insert('end', f'[{format_time(seg.start)} {label}]  ✎\n', (hdr_tag, 'protected_header'))
            self._inner.insert('end', seg.text + '\n')
        self._inner.tag_config('protected_header', background=_BG_RIGHT)
        self._inner.config(undo=True, maxundo=-1)
        self._inner.edit_reset()  # clear pre-edit history

    def _guard_header(self, event):
        if not self._edit_mode: return None
        if event.state & 8: return None  # Command key: let undo/redo/etc. through
        inner = self._inner
        try:
            s = inner.index('sel.first'); e = inner.index('sel.last')
            if inner.tag_nextrange('protected_header', s, e): return 'break'
            if 'protected_header' in inner.tag_names(s): return 'break'
        except tk.TclError: pass
        cur = inner.index('insert')
        if event.keysym == 'BackSpace':
            try:
                if 'protected_header' in inner.tag_names(f'{cur}-1c'): return 'break'
            except tk.TclError: pass
        elif 'protected_header' in inner.tag_names(cur): return 'break'
        return None

    def _exit_edit_mode(self, save=True):
        self._inner.config(undo=False)
        self._inner.edit_reset()
        for ev, bid in (('<Key>', getattr(self,'_gbid_key',None)), ('<Delete>', getattr(self,'_gbid_del',None)),
                        ('<<Paste>>', getattr(self,'_gbid_paste',None)), ('<<Cut>>', getattr(self,'_gbid_cut',None))):
            if bid:
                try: self._inner.unbind(ev, bid)
                except: pass
        self._inner.tag_delete('protected_header')
        if save: self._sync_edits_to_segments()
        self._edit_mode = False
        self._textbox.configure(state='disabled')
        self._edit_btn.configure(text='編集', fg_color='#FFFFFF', text_color=_TEXT, hover_color=_BG_CARD, border_width=1)
        if save and self._segments: self._render(self._segments)
        for b in (self._txt_btn, self._srt_btn, self._json_btn, self._save_proj_btn): b.configure(state='normal')

    def _sync_edits_to_segments(self):
        inner = self._inner
        header_ranges = []
        idx = '1.0'
        while True:
            r = inner.tag_nextrange('protected_header', idx)
            if not r: break
            header_ranges.append(r); idx = r[1]
        if len(header_ranges) != len(self._segments):
            messagebox.showwarning('編集エラー',
                'ヘッダー行が変更されています。\n話者ラベル行は変更しないでください。')
            return
        for i, seg in enumerate(self._segments):
            ts = header_ranges[i][1]
            te = header_ranges[i+1][0] if i+1 < len(header_ranges) else 'end'
            seg.text = inner.get(ts, te).strip()

    def _copy_to_clipboard(self):
        if not self._segments: return
        self.clipboard_clear(); self.clipboard_append(segments_to_txt(self._segments))
        self._copy_btn.configure(text="\u2713 \u5b8c\u4e86"); self.after(2000,lambda:self._copy_btn.configure(text="\u30b3\u30d4\u30fc"))
    def _save_project(self):
        if not self._segments: return
        stem=Path(self._current_file).stem if self._current_file else "project"
        path=filedialog.asksaveasfilename(defaultextension=".transcription",initialfile=f"{stem}.transcription",filetypes=[("Transcription Project","*.transcription"),("JSON","*.json"),("\u3059\u3079\u3066\u306e\u30d5\u30a1\u30a4\u30eb","*.*")])
        if not path: return
        Path(path).write_text(json.dumps({"version":1,"audio_file":self._current_file or "","speaker_names":self._speaker_names,"segments":[{"start":s.start,"end":s.end,"speaker":s.speaker,"display_name":s.display_name,"text":s.text} for s in self._segments]},ensure_ascii=False,indent=2),encoding="utf-8")
        messagebox.showinfo("\u4fdd\u5b58\u5b8c\u4e86",f"\u30d7\u30ed\u30b8\u30a7\u30af\u30c8\u3092\u4fdd\u5b58\u3057\u307e\u3057\u305f:\n{path}")
    def _load_project(self):
        path=filedialog.askopenfilename(filetypes=[("Transcription Project","*.transcription"),("JSON","*.json"),("\u3059\u3079\u3066\u306e\u30d5\u30a1\u30a4\u30eb","*.*")])
        if not path: return
        try: data=json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc: messagebox.showerror("\u30a8\u30e9\u30fc",f"\u8aad\u307f\u8fbc\u3081\u307e\u305b\u3093\u3067\u3057\u305f:\n{exc}"); return
        self._current_file=data.get("audio_file") or None; self._speaker_names=data.get("speaker_names",{}); self._speaker_colors={}
        self._segments=[Segment(start=s["start"],end=s["end"],speaker=s["speaker"],text=s["text"],display_name=s.get("display_name","")) for s in data.get("segments",[])]
        if self._edit_mode:
            self._edit_mode=False; self._textbox.configure(state="disabled")
            self._edit_btn.configure(text="\u7de8\u96c6",fg_color="#FFFFFF",text_color=_TEXT,hover_color=_BG_CARD,border_width=1)
        if self._segments:
            self._render(self._segments); self._set_export_state("normal")
            self._start_btn.configure(state="normal" if self._current_file else "disabled")
            self._progress_label.configure(text=f"\u30d7\u30ed\u30b8\u30a7\u30af\u30c8\u8aad\u307f\u8fbc\u307f\u5b8c\u4e86  ({len(self._segments)} \u30d6\u30ed\u30c3\u30af)")
        else: self._show_placeholder("\u30bb\u30b0\u30e1\u30f3\u30c8\u304c\u898b\u3064\u304b\u308a\u307e\u305b\u3093\u3067\u3057\u305f\u3002")
    def _set_export_state(self,state):
        for b in (self._txt_btn,self._srt_btn,self._json_btn,self._copy_btn,self._edit_btn,self._save_proj_btn): b.configure(state=state)
    def _export(self,fmt):
        if not self._segments: return
        stem=Path(self._current_file).stem if self._current_file else "transcript"
        ext={"txt":".txt","srt":".srt","json":".json"}[fmt]
        path=filedialog.asksaveasfilename(defaultextension=ext,initialfile=f"{stem}{ext}",filetypes=[(fmt.upper(),f"*{ext}"),("\u3059\u3079\u3066\u306e\u30d5\u30a1\u30a4\u30eb","*.*")])
        if not path: return
        Path(path).write_text({"txt":segments_to_txt,"srt":segments_to_srt,"json":segments_to_json}[fmt](self._segments),encoding="utf-8")
        messagebox.showinfo("\u4fdd\u5b58\u5b8c\u4e86",f"\u4fdd\u5b58\u3057\u307e\u3057\u305f:\n{path}")

def main():
    ctk.set_appearance_mode("light"); ctk.set_default_color_theme("blue")
    App().mainloop()

if __name__=="__main__": main()
