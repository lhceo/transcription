# プロジェクト概要（Claude 用）

このファイルは新しい Claude セッションが状況を即把握するための道しるべです。詳細は `docs/` 配下を参照してください。

## アプリの目的

社内会議・お客様打ち合わせの音声を **話者分離付きで文字起こし** するアプリ。  
Notta（年20万円規模の有料サービス）からの脱却が動機。

## 現在のステータス（2026-05-12 時点）

- ✅ **Web アプリ v1.0 リリース完了** — Railway で本番稼働中
- 本番 URL: `https://transcription-production-f1c1.up.railway.app`
- 現在ブランチ: `web-app-rewrite`（main ではない）／最新タグ: `v1.0-production`
- Mac アプリ版は `v0.1-mac-app-snapshot` タグで凍結済み（参照のみ）
- フェーズ 1〜8 すべて完了。次は **v1.1（音声同期再生）** を計画中
- 既知の改善候補は `docs/ROADMAP.md` の「v1.0.1 以降の改善候補」セクション参照

### 直近で実装済みの v1.0 機能
- Google OAuth (lionheart.co.jp ドメイン限定)
- 音声アップロード (mp3/mp4, 最大 2GB / 約2時間)
- AssemblyAI 経由の日本語文字起こし + 話者分離
- セグメント編集 / Shift+Return で分割 / TXT・SRT・JSON エクスポート
- 話者リネーム (Notta 方式: 個別 ／ すべてに適用、同名禁止＋候補選択は例外)
- 同じ表示名は同じ色（出現順 8 色循環）

### 次セッション開始時のチェック
1. `git status` / `git log --oneline -10` で現状確認
2. `docs/ROADMAP.md` 末尾の「次のアクション」と「v1.0.1 以降の改善候補」を確認
3. `docs/DECISIONS.md` 冒頭で直近の判断を確認
4. 必要なら本番 URL をシークレットウィンドウで開いて挙動確認

## 重要な前提

1. **ユーザーは非エンジニア**。手順はコピペで完結する形に、専門用語を表面に出さない
2. **配布対象は社内 5〜8人**（小規模）
3. **時間はかけても良い**（急がない）。じっくり丁寧に作る
4. **メイン用途は日本語音声**、最大2時間想定
5. **お客様情報を含む音声**を扱うので、機密性に配慮する
6. **コスト目標**: Notta（年15-20万）の何分の1かに抑える

## 技術スタック（v1.0 確定版）

| レイヤ | 採用 |
|---|---|
| UI | Jinja2 テンプレート + Alpine.js + HTMX |
| バックエンド | FastAPI + Uvicorn (`--proxy-headers` + Railway HTTPS 終端) |
| 文字起こし / 話者分離 | **AssemblyAI** (クラウド API、処理後音声は即削除 A-4) |
| 認証 | **Google OAuth** (authlib + Starlette SessionMiddleware, 14日 Cookie) |
| ホスティング | **Railway** (Railpack, 永続ボリューム `/data` に SQLite) |
| データ永続化 | SQLite + Alembic (FastAPI lifespan で起動時に `upgrade head`) |
| デプロイ | `Procfile` の web: のみ。release: は削除済み (lifespan で代替) |

## 開発の進め方ルール

1. **Git タグでチェックポイント運用**：機能ごとに動く状態を `v0.X-機能名` でタグ
2. **Mac アプリ版は別ブランチに凍結**：`web-app-rewrite` ブランチで Web 版を開発
3. **ファイル分割を意識**：1ファイルに 2,000行 詰め込まない。機能ごとに分ける
4. **設計判断は `docs/DECISIONS.md` に記録**
5. **実装の進捗は `docs/ROADMAP.md` に追記**

## 詳しい情報

- **設計の全決定**: `docs/DESIGN.md`（A〜F の全6カテゴリ）
- **判断の経緯**: `docs/DECISIONS.md`（なぜそうしたかの履歴）
- **実装計画**: `docs/ROADMAP.md`（次やること）
- **変更履歴**: `docs/CHANGELOG.md`
- **ビルド手順（旧）**: `BUILD.md`（Mac アプリ版時代の手順、Web 版実装後に更新予定）

## ユーザーとの会話スタイル

- ユーザーは「話があちこちいくタイプ」と自認。トピックの飛びを歓迎する
- 設計判断では「第三者エンジニア視点 + ユーザー視点」での多角的検討を好む
- 重要な判断の前には「立ち止まって設計レビュー」を希望される
- 急がず、丁寧に進めることを優先する
