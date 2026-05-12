# Railway デプロイ手順（DEPLOY.md）

このファイルは本アプリを Railway にデプロイする手順です。  
社内向けの本番運用ガイドとして利用します。

最終更新: 2026-05-12

---

## 前提

- GitHub にこのリポジトリが push されている（ブランチ: `web-app-rewrite` または `main`）
- Google Cloud Console で OAuth クライアントを作成済み（Phase 2 の手順参照）
- AssemblyAI で API キー取得済み（Phase 4 の手順参照）

---

## ステップ 1: Railway アカウント作成

1. https://railway.app/ にアクセス
2. **「Login」→「Login with GitHub」** で GitHub アカウントでログイン
3. メールアドレス確認等

無料枠: 月 $5 のクレジット付き（小規模ツールなら無料枠内で運用可能）

---

## ステップ 2: 新規プロジェクト作成

1. ダッシュボードで **「New Project」** をクリック
2. **「Deploy from GitHub repo」** を選択
3. GitHub 連携を許可し、このリポジトリを選択
4. ブランチを選択（`web-app-rewrite` または `main`）
5. **「Deploy Now」** をクリック

Railway が自動で:
- pyproject.toml を検出して Python プロジェクトと認識
- 依存をインストール
- Procfile に従って `alembic upgrade head` → `uvicorn` を実行

最初のデプロイは数分かかります。失敗しても次のステップで環境変数を設定して再デプロイすれば OK。

---

## ステップ 3: 永続ボリュームの設定

SQLite ファイルが再デプロイで消えないようボリュームを追加します。

1. プロジェクト画面のサービス（自動生成されたもの）をクリック
2. **「Settings」タブ → 「Volumes」** にスクロール
3. **「+ New Volume」** をクリック
4. Mount path: **`/data`**
5. Save

---

## ステップ 4: 環境変数の設定

サービス画面の **「Variables」タブ** で以下を設定:

### 必須

| 変数名 | 値 | 備考 |
|---|---|---|
| `APP_ENV` | `production` | Cookie の Secure 強制 |
| `SESSION_SECRET` | （生成した強力な文字列） | Python で生成: `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `ALLOWED_EMAIL_DOMAINS` | `lionheart.co.jp` | 社内ドメイン |
| `GOOGLE_OAUTH_CLIENT_ID` | （Google Cloud Console の値） | |
| `GOOGLE_OAUTH_CLIENT_SECRET` | （同上） | |
| `GOOGLE_OAUTH_REDIRECT_URI` | `https://<your-app>.up.railway.app/auth/google/callback` | Railway の公開 URL は次のステップで取得 |
| `ASSEMBLYAI_API_KEY` | （AssemblyAI の API キー） | |
| `DATABASE_URL` | `sqlite:////data/app.db` | スラッシュ4つに注意 |
| `UPLOAD_TMP_DIR` | `/tmp/transcription_uploads` | 一時保存先 |

設定が終わったら自動で再デプロイされます。

---

## ステップ 5: Railway 公開ドメインの取得と Google OAuth 更新

1. サービスの **「Settings」 → 「Networking」**
2. **「Generate Domain」** をクリックすると `https://your-app.up.railway.app` のような URL が発行される
3. その URL をコピー

### Google Cloud Console 側の更新

1. https://console.cloud.google.com/ にアクセス
2. プロジェクト `transcription-app` を選択
3. **「APIs & Services」→「OAuth クライアント」**
4. クライアント「Transcription Web (Local Dev)」をクリック（名前は適宜変更可）
5. **承認済みのリダイレクト URI** に以下を追加:
   - `https://<your-app>.up.railway.app/auth/google/callback`
6. 保存

### Railway 側の更新

`GOOGLE_OAUTH_REDIRECT_URI` を上記の URL に上書きして保存。

---

## ステップ 6: 動作確認

1. Railway 発行の URL をブラウザで開く
2. ログイン画面 → Google でサインイン
3. アップロード → 文字起こし → 編集 → エクスポートを試す
4. 履歴の永続化（ボリューム）が機能していることを再デプロイ後に確認

---

## ステップ 7: 社員への配布

メールや Slack で URL を共有:

```
社内用の文字起こしアプリ:
https://<your-app>.up.railway.app

社内アカウント（@lionheart.co.jp）でログインしてご利用ください。
```

各自で：
- ブラウザでアクセス
- 「Google でサインイン」
- mp3 / mp4 ファイルをアップロード
- 文字起こし結果を確認・編集・エクスポート

---

## 運用上の注意

### AssemblyAI 月間支出上限の設定（C-1）

万一 API キーが流出しても被害が限定されるよう、AssemblyAI ダッシュボードで月の支出上限を設定:

1. https://www.assemblyai.com/dashboard/ にログイン
2. Account / Billing 設定
3. **Monthly Usage Limit** を **$35（約 5,000円）** などに設定

### コスト監視

- Railway のダッシュボードで毎月の利用料金を確認（通常 $5 〜 $10 程度）
- AssemblyAI のダッシュボードで処理時間と料金を確認

### バックアップ

Railway のボリュームは Railway 側でバックアップされていますが、念のため定期的に DB をダウンロードすることを推奨：

```bash
# Railway CLI で接続してダウンロード（要 CLI セットアップ）
railway run cat /data/app.db > backup_$(date +%Y%m%d).db
```

### 退職者対応

退職者が出たら：
1. Google Workspace 管理画面で該当アカウントを停止
2. これで該当ユーザーは自動的にログイン不可（OAuth 失敗）
3. 必要に応じて Railway の DB から該当ユーザーのデータを削除

---

## トラブルシューティング

### デプロイが失敗する
- 「Logs」タブで詳細を確認
- 依存インストール失敗 → pyproject.toml の Python バージョンを確認（`requires-python = ">=3.12"`）
- マイグレーション失敗 → DB パスの設定を確認

### ログインできない（Google OAuth エラー）
- リダイレクト URI が Google Cloud Console と Railway で一致しているか確認
- `ALLOWED_EMAIL_DOMAINS` の値を確認

### 文字起こしが「失敗」になる
- `ASSEMBLYAI_API_KEY` が正しく設定されているか
- AssemblyAI ダッシュボードで利用上限に達していないか

### DB が消える
- ボリュームが `/data` にマウントされているか確認
- `DATABASE_URL=sqlite:////data/app.db`（スラッシュ4つ）か確認
