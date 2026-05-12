# 変更履歴（CHANGELOG.md）

このファイルはバージョンごとの変更内容を記録します。  
形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に準拠。

---

## [Unreleased] - 2026-05-12

### 追加
- `CLAUDE.md`（Claude セッション継続用の道しるべ）
- `docs/DESIGN.md`（全設計決定）
- `docs/DECISIONS.md`（判断の経緯記録）
- `docs/ROADMAP.md`（実装ロードマップ）
- `docs/CHANGELOG.md`（本ファイル）

### 設計上の決定
- **方針転換**: Mac アプリ → Web アプリへ全面書き換え
- **文字起こし方式**: ローカル ML（mlx-whisper + pyannote）→ AssemblyAI クラウド API
- **認証**: Google SSO（Google Workspace 連携）
- **ホスティング**: マネージドクラウドサービス
- **話者編集 UI**: Notta 風ドロップダウンに改修予定

### 既知の課題
- `transcribe_core.py` に未コミットの MPS 復帰実験コードが残っている（Web アプリ化で破棄予定）

---

## [v0.1-mac-app-snapshot] - 2026-05-11（タグ予定）

### Mac アプリ版の最終状態

社内未配布のまま、Web アプリへ全面書き換えする前のスナップショット。

#### 主要機能
- Tkinter (customtkinter) ベースのデスクトップ UI
- mlx-whisper による日本語文字起こし
- pyannote-audio による話者分離
- 編集機能（テキスト編集・話者リネーム）
- エクスポート（TXT / SRT / JSON）
- プロジェクト保存・読込
- Undo / Redo
- macOS Keychain による HuggingFace Token 保存
- py2app による .app パッケージ化

#### 既知の問題（書き換えの動機）
- 2時間音声で 16GB Mac が OOM クラッシュ
- pyannote の MPS / CPU 切替で迷走（速度と安定性のトレードオフ）
- 配布先 Mac のスペック差を吸収できない
- 外出先での使用に向かない設計
- 全 2,191 行が `app.py` に集約されており、保守性に課題

---

## 今後の予定

### [v0.2-web-skeleton]（次のマイルストーン）
Web アプリの最小骨格

### [v0.3-auth]
Google SSO 認証

### [v0.4-upload]
ファイルアップロード機能

### [v0.5-transcription]
AssemblyAI 連携・文字起こし機能

### [v0.6-results]
結果表示・編集機能

### [v0.7-history]
履歴一覧機能

### [v0.8-operations]
コスト管理・運用機能

### [v1.0-production]
本番デプロイ・社内提供開始
