# 管理者向け操作メモ（ADMIN_OPERATIONS.md）

このドキュメントは、本アプリの **管理者（現状は市川さん 1 名）** が運用で行う操作の手順をまとめたチートシートです。半年後の自分・新しい引き継ぎ担当者が見ても迷わない粒度で書いています。

最終更新: 2026-05-12（v1.0.2 時点）

---

## 1. アプリの構成（前提知識）

| 要素 | 場所 |
|---|---|
| 本番 URL | https://transcription-production-f1c1.up.railway.app |
| ホスティング | Railway (https://railway.com) |
| GitHub リポジトリ | https://github.com/lhceo/transcription |
| 本番ブランチ | `web-app-rewrite` |
| 文字起こし API | AssemblyAI (https://www.assemblyai.com/) |
| 認証 | Google OAuth (`@lionheart.co.jp` ドメインのみ許可) |
| DB | SQLite (`/data/app.db`、Railway の永続ボリューム) |
| 音声ファイル保管 | `/data/audio/{transcript_id}.{mp3 or mp4}` |

設定値はすべて **Railway の Variables** で管理しています。コードに直書きしていません。

---

## 2. Railway の基本操作

### 2.1 ログイン

1. https://railway.com を開く
2. 市川さんのアカウントでログイン
3. プロジェクト一覧から **`transcription`** を選択
4. プロジェクト内のサービス（`transcription` という名前のはず）をクリック

サービス画面の上部に **Deployments / Variables / Settings / Logs / Metrics** などのタブが並んでいます。

### 2.2 環境変数を変更する

1. **Variables** タブをクリック
2. 既存の変数を変更したい場合: 行の右の鉛筆アイコンをクリック → 値を編集 → **Save**
3. 新規追加: 右上の **+ New Variable** をクリック → Name と Value を入力 → **Add**
4. 保存すると自動で再デプロイが始まる（1〜2 分）

### 2.3 デプロイ状況を見る

1. **Deployments** タブをクリック
2. 一番上が最新のデプロイ
3. ステータスが **Success** なら本番反映済み。**Building** 中は待つ。**Crashed** はエラー
4. クリックすると詳細ログを見られる

### 2.4 ログを見る（エラー調査用）

1. **Deployments** タブで現在のデプロイをクリック
2. または **Logs** タブで現在動いているアプリのログをリアルタイム表示
3. 「ユーザーがエラーを報告した時刻」をたよりに、その付近の行を探す

---

## 3. 環境変数の一覧（参考）

現在設定している（または設定するべき）環境変数です。

| 変数名 | 役割 | 例 |
|---|---|---|
| `APP_ENV` | 環境名 | `production` |
| `SESSION_SECRET` | セッション暗号化キー（変えると全員ログアウト） | ランダム文字列 |
| `ALLOWED_EMAIL_DOMAINS` | ログイン許可ドメイン | `lionheart.co.jp` |
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth クライアント ID | (Google Cloud Console から) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth シークレット | (同上) |
| `GOOGLE_OAUTH_REDIRECT_URI` | OAuth コールバック URL | `https://transcription-production-f1c1.up.railway.app/auth/google/callback` |
| `ASSEMBLYAI_API_KEY` | 文字起こし API キー | (AssemblyAI ダッシュボードから) |
| `MONTHLY_COST_LIMIT_YEN` | 月次コスト上限 (円、0 で無制限) | `5000` |
| `AUDIO_STORAGE_DIR` | 音声ファイルの保管先 | `/data/audio` |
| `DATABASE_URL` | DB 接続文字列 | `sqlite:////data/app.db` |

---

## 4. コスト管理

### 4.1 今月の利用額を確認する

- アプリの **ダッシュボード上部**（ログイン直後の画面）に「今月の利用額 ¥X / ¥Y」と表示されています
- ¥X が現在値、¥Y が月次上限（環境変数 `MONTHLY_COST_LIMIT_YEN` の値）
- バーが緑 = 余裕、黄 = 80% 超え、赤 = 上限到達

### 4.2 月次上限額を変える

1. Railway の **Variables** で `MONTHLY_COST_LIMIT_YEN` を編集
2. 値の単位は **円**。例: `5000` で月 5,000 円
3. `0` または変数を削除すると **無制限**
4. 保存後、自動再デプロイ → ダッシュボードに反映

### 4.3 AssemblyAI 側の実際の請求を確認する

1. https://www.assemblyai.com/app にログイン
2. 左サイドバー の **Usage / Billing** で実際の使用量・請求額を見られる
3. アプリ内表示は「概算（音声長 × 単価）」なので、実請求と若干ずれることあり

### 4.4 コストが想定外に増えた時の調査

1. ダッシュボードに表示される履歴で、当月の文字起こし一覧を確認
2. 長時間の音声 / 大量アップロード があったら、それが原因
3. Railway の Logs で「`Transcript レコード作成`」のログ行を時系列で見ると、いつ誰がアップロードしたかが追える

---

## 5. データ管理

### 5.1 重要なファイルの場所（本番）

| 種類 | パス | 用途 |
|---|---|---|
| DB 本体 | `/data/app.db` | 全文字起こし結果・ユーザー情報・話者履歴 |
| 音声ファイル | `/data/audio/{transcript_id}.{ext}` | 再生用 |
| 一時アップロード | `/tmp/...` | 処理中のみ、すぐ消える |

`/data` は Railway の **永続ボリューム**。コンテナを再デプロイしても消えません。

### 5.2 バックアップ（手動、推奨頻度: 月 1 回）

⚠️ **現状、自動バックアップはありません**。重要データが増えてきたら手動で取ってください。

1. Railway のサービス画面 → **Settings** タブ
2. 「Volume」セクションから手動でバックアップを取れる場合があります（Railway の機能仕様による）
3. もしくは、Railway CLI で `railway run` 経由で `/data/app.db` をダウンロードする方法もあります

将来的には **自動バックアップ機能の追加**（音声・DB を S3 等に日次転送）も検討候補です（DECISIONS.md 参照）。

### 5.3 古い音声を削除する（容量節約）

現状、音声ファイルは **手動削除のみ**:
- ユーザーが詳細画面の「音声を削除」リンクから個別削除
- 文字起こし全体を削除すると音声も一緒に消える

将来的には自動削除（30 日経過したら削除など）の追加も計画中（DECISIONS.md の v1.0.1 改善候補参照）。

---

## 6. アクセス管理

### 6.1 ログインできる人

`ALLOWED_EMAIL_DOMAINS` の値（現状 `lionheart.co.jp`）に含まれるドメインのメールアドレスを持つ Google アカウントだけがログインできます。

### 6.2 特定のドメインを追加する

1. Railway の Variables で `ALLOWED_EMAIL_DOMAINS` を編集
2. カンマ区切りで複数指定可能。例: `lionheart.co.jp,partner.co.jp`
3. 保存 → 自動再デプロイ

### 6.3 特定の人をブロックしたい

現状、メールアドレス単位のブロック機能はありません。やるなら:
- 該当ユーザーの Google アカウントの所属を組織管理者に依頼
- もしくは将来的にコードでブロックリスト機能を追加

### 6.4 新しいスタッフに使ってもらう

特別な手続きは不要です。`@lionheart.co.jp` の Google アカウントを持っていれば、本番 URL を開いて「Google でサインイン」すれば自動でユーザー作成されます。

---

## 7. トラブルシューティング

### 7.1 デプロイがコケた

1. Railway の **Deployments** タブで最新の失敗デプロイをクリック
2. ログに表示されたエラーメッセージを確認
3. よくある原因:
   - 環境変数の設定漏れ（例: `GOOGLE_OAUTH_CLIENT_SECRET` が空）
   - 依存パッケージのバージョン非互換（`requirements.txt` 変更時）
   - Alembic マイグレーション失敗（`docs/DESIGN.md` の D-5 参照）
4. 自分で解決できない場合: GitHub の Actions タブで CI ログを見る、または Claude に相談

### 7.2 ユーザーが「ログインできない」と言ってきた

1. メールアドレスのドメインが `ALLOWED_EMAIL_DOMAINS` に含まれているか確認
2. Google アカウントのセッションが期限切れ → Google からログアウト → 再ログイン
3. Cookie ブロックされていないか確認（特にシークレットウィンドウ）
4. Railway の Logs で `auth/google` 関連のエラーを探す

### 7.3 アップロードが「失敗」になる

1. ファイル形式が `.mp3` / `.mp4` か確認（他の形式は弾かれます）
2. サイズが 2GB 以下か確認
3. AssemblyAI 側で API キー失効・残高不足の可能性 → AssemblyAI ダッシュボードで確認
4. Railway の Logs で「`AssemblyAI 呼び出しで失敗`」を検索

### 7.4 「処理中」のまま終わらない

- 通常、音声長の 1/15〜1/25 で完了します（高精度モード基準で 1 時間音声 = 約 4 分）
- 30 分以上「処理中」が続いている場合、AssemblyAI 側で詰まっている可能性
- 6 時間でタイムアウト判定して「失敗」になる仕組みあり

---

## 8. 関連リソース

| 用途 | URL / 場所 |
|---|---|
| Railway ダッシュボード | https://railway.com |
| AssemblyAI ダッシュボード | https://www.assemblyai.com/app |
| Google Cloud Console (OAuth) | https://console.cloud.google.com |
| GitHub リポジトリ | https://github.com/lhceo/transcription |
| 本アプリの設計判断履歴 | `docs/DECISIONS.md` |
| 機能ロードマップ | `docs/ROADMAP.md` |
| 変更履歴 | `docs/CHANGELOG.md` |

---

## 9. 将来の運用課題（メモ）

実装は未着手だが、運用が成熟するにつれて検討したい項目:

- **管理者ページ `/admin`**: ユーザーごとの利用状況確認、上限変更を GUI から（設計合意済み、未実装）
- **自動バックアップ**: SQLite と音声を日次で外部に転送
- **音声の自動削除**: 30 日後など期限を切って自動削除
- **コスト警告メール**: 上限の 80% 到達時に管理者へ通知
