"""Persistent app preferences and HF Token storage.

Two cooperating stores:
  - ``~/.transcription_app/config.json`` for non-sensitive UI preferences
    (model name, language, diarization on/off, window geometry, etc.).
    Writes are atomic (tmp → rename) and a corrupt JSON is renamed to
    ``config.broken.json`` instead of being silently dropped.
  - macOS Keychain for the HuggingFace Token. The Keychain entry is
    keyed by the .app bundle identifier so it shows up clearly in
    Keychain Access.app.

Public surface:
  - ``CONFIG_PATH``: Path to the JSON preferences file.
  - ``load_config()`` / ``save_config(cfg)``: read / write preferences.
  - ``KEYCHAIN_SERVICE`` / ``KEYCHAIN_USER_HF``: identifiers for the HF
    Token entry in the Keychain.
  - ``load_hf_token()`` / ``save_hf_token(token)``: read / write the HF
    Token, with automatic migration of legacy plaintext tokens that
    older versions of the app stored in ``config.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


# ── Preferences (config.json) ────────────────────────────────────────────────
CONFIG_PATH = Path.home() / ".transcription_app" / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config root is not a dict")
        return data
    except Exception:
        # 壊れた JSON は捨てる前に .broken にリネームして退避。
        # 次回保存時に新しいファイルが作られるが、復旧したい時のために残す。
        try:
            backup = CONFIG_PATH.with_suffix(".broken.json")
            CONFIG_PATH.replace(backup)
        except Exception:
            pass
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg.setdefault("version", 1)
    # アトミックに書く: tmp → rename。途中でアプリが落ちても本ファイルが壊れない。
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


# ── Keychain (HuggingFace Token) ─────────────────────────────────────────────
# HF Token は config.json に平文ではなく macOS Keychain に保存する。
# Service 名は .app の bundle identifier に揃える（Keychain Access.app で
# 見たときに用途が分かりやすいように）。
KEYCHAIN_SERVICE = "co.lionheart.noto"
KEYCHAIN_USER_HF = "hf_token"


def load_hf_token() -> str:
    """HF Token の取得順序:
    1. macOS Keychain
    2. 旧 config.json の平文（あれば Keychain へ移行して config から削除）
    3. 環境変数 HF_TOKEN
    4. 空文字
    """
    try:
        import keyring  # type: ignore
        tok = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER_HF)
        if tok:
            return tok
    except Exception:
        pass

    # 旧形式（config.json に平文保存）の救済 + Keychain へ移行
    cfg = load_config()
    legacy = cfg.get("hf_token")
    if isinstance(legacy, str) and legacy:
        if save_hf_token(legacy):
            cfg.pop("hf_token", None)
            try:
                save_config(cfg)
            except Exception:
                pass
        return legacy

    return os.environ.get("HF_TOKEN", "")


def save_hf_token(token: str) -> bool:
    """HF Token を macOS Keychain に保存。空文字なら既存エントリを削除。
    keyring が使えない環境では False を返す（呼び出し元で fallback 可）。"""
    try:
        import keyring  # type: ignore
        if token:
            keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USER_HF, token)
        else:
            try:
                keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_USER_HF)
            except Exception:
                # 既存エントリが無い場合の PasswordDeleteError は無視
                pass
        return True
    except Exception:
        return False
