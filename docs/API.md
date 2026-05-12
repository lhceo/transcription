# API 仕様（API.md）

このファイルは Web アプリの API 設計と主要エンドポイントを記述したものです。

**重要**: 実装は FastAPI を使うため、コードから OpenAPI 仕様が **自動生成** されます（`/docs` で参照可能）。  
この `API.md` は **「設計の意図」** と **「全体像」** を伝える人間向けドキュメントです。  
詳細なリクエスト/レスポンスの形は実装後の自動生成ドキュメントが正典です。

最終更新: 2026-05-12

---

## 1. API 設計の方針

### 基本ルール

- **REST 風**：HTTP メソッド（GET/POST/PATCH/DELETE）と URL でリソースを操作
- **JSON 応答**：`/api/*` エンドポイントは JSON を返す
- **HTML 応答**：それ以外のパス（`/`, `/transcripts/{id}` など）はサーバーレンダリングの HTML
- **HTMX 親和**：HTMX が呼び出すエンドポイントは HTML 断片を返すこともある
- **認証必須**：`/health`, `/login`, `/auth/*` 以外はログイン必須
- **同一オリジン前提**：API キー認証ではなく、セッション Cookie 認証

### URL の命名

| パターン | 用途 |
|---|---|
| `/` | ホーム（履歴一覧） |
| `/login` | ログイン画面 |
| `/auth/google` | Google OAuth 開始 |
| `/transcripts/{id}` | 文字起こし詳細ページ |
| `/settings` | 設定画面 |
| `/api/...` | JSON を返す API |
| `/health` | 死活監視用 |

---

## 2. 認証関連エンドポイント

### `GET /login`

**用途**: ログイン画面を表示。「Google でログイン」ボタンを置く。

**応答**: HTML

**未認証ユーザーがアクセスした場合**: このページを直接表示  
**認証済みユーザーがアクセスした場合**: `/` にリダイレクト

---

### `GET /auth/google`

**用途**: Google OAuth フローを開始。ユーザーを Google の認証ページにリダイレクト。

**動作**:
1. `state` パラメータをランダム生成してセッションに保存（CSRF 防止）
2. Google OAuth の認可エンドポイントにリダイレクト
3. リダイレクト先 URL: `/auth/google/callback`

---

### `GET /auth/google/callback`

**用途**: Google OAuth のコールバック受信。

**クエリパラメータ**:
- `code` — 認可コード
- `state` — CSRF 対策トークン

**動作**:
1. `state` を検証（セッションに保存したものと一致するか）
2. `code` を Google に送って ID トークンを取得
3. ID トークンの `email` から **ドメイン検証**（社内ドメイン以外は弾く）
4. `users` テーブルに既存ユーザーがいれば更新、なければ作成
5. セッション Cookie 発行
6. `/` にリダイレクト

**エラー時**: `/login?error=...` にリダイレクト

---

### `POST /auth/logout`

**用途**: ログアウト

**動作**: セッション Cookie を削除して `/login` にリダイレクト

---

## 3. ページエンドポイント（HTML 応答）

### `GET /`

**用途**: ホーム（履歴一覧 + アップロード入口）

**応答**: HTML（過去の文字起こし一覧、新規アップロードボタン）

**含まれる情報**:
- ユーザーの文字起こし一覧（最新順）
- 各項目に：ファイル名、日付、時間、話者数、ステータス
- 「新規アップロード」ボタン
- 設定アイコン（→ `/settings`）

### `GET /transcripts/{id}`

**用途**: 特定の文字起こしの詳細表示・編集

**応答**: HTML（カード型の発言一覧、編集 UI）

**権限**: そのジョブの所有者のみ閲覧可能（他人のものは 404）

**ステータス別の表示**:
- `uploading`: アップロード進捗
- `processing`: 進捗バー＋推定残り時間
- `completed`: 結果表示＋編集 UI
- `failed`: エラーメッセージ＋再試行ボタン

### `GET /settings`

**用途**: 設定画面

**含まれる項目**:
- ユーザー情報（メール、名前）
- モデル選択（高精度／コスト重視）
- 月次利用状況（処理時間、概算料金、件数）
- 話者履歴の管理（将来）
- ログアウトボタン

---

## 4. JSON API エンドポイント

### 4.1 文字起こしジョブ

#### `POST /api/transcripts`

**用途**: 新規ジョブ作成（ファイルアップロード）

**リクエスト**: `multipart/form-data`
- `file`: 音声ファイル（必須）
- `model_tier`: `best` または `nano`（デフォルト `best`）

**応答**: `201 Created`
```json
{
  "id": 123,
  "status": "uploading",
  "original_filename": "会議録音_20260512.mp3",
  "file_size_bytes": 12345678,
  "created_at": "2026-05-12T10:30:00Z"
}
```

**エラー**:
- `400`: ファイルが空、ファイル名がない
- `413`: 2GB 上限超過
- `415`: 非対応形式
- `429`: レート制限超過（将来）

#### `GET /api/transcripts`

**用途**: ユーザーの文字起こし一覧取得（HTMX や JS 側の動的更新用）

**クエリパラメータ**:
- `limit`: 取得件数（デフォルト 50）
- `offset`: スキップ数（ページネーション用）
- `status`: フィルタ（複数指定可、例: `completed,processing`）

**応答**: `200 OK`
```json
{
  "items": [
    {
      "id": 123,
      "original_filename": "...",
      "status": "completed",
      "audio_duration_seconds": 3600,
      "speaker_count": 3,
      "cost_yen": 110,
      "created_at": "...",
      "completed_at": "..."
    },
    ...
  ],
  "total": 42,
  "limit": 50,
  "offset": 0
}
```

#### `GET /api/transcripts/{id}`

**用途**: 1件の詳細取得（segments と speakers を含む）

**応答**: `200 OK`
```json
{
  "id": 123,
  "original_filename": "...",
  "status": "completed",
  "audio_duration_seconds": 3600,
  "cost_yen": 110,
  "speakers": [
    {"speaker_label": "SPEAKER_00", "display_name": "山田さん", "color": "#3B82F6"},
    {"speaker_label": "SPEAKER_01", "display_name": "未設定話者2", "color": "#10B981"}
  ],
  "segments": [
    {
      "id": 4567,
      "order_index": 0,
      "start_seconds": 0.0,
      "end_seconds": 3.5,
      "speaker_label": "SPEAKER_00",
      "display_name": null,
      "text": "こんにちは。",
      "is_edited": false
    },
    ...
  ],
  "created_at": "...",
  "completed_at": "..."
}
```

#### `GET /api/transcripts/{id}/status`

**用途**: フロントエンドがジョブ進捗をポーリングするための軽量エンドポイント

**応答**: `200 OK`
```json
{
  "id": 123,
  "status": "processing",
  "progress_percent": 45,
  "estimated_remaining_seconds": 240
}
```

**ポーリング頻度の目安**: 3〜5秒間隔（フロントエンド側で実装）

#### `DELETE /api/transcripts/{id}`

**用途**: 文字起こしを削除（ソフト削除）

**応答**: `204 No Content`

**動作**: `deleted_at` に現在時刻を入れる。一覧から非表示になる。30日後にハードDelete（将来）。

#### `GET /api/transcripts/{id}/export`

**用途**: テキスト形式でエクスポート

**クエリパラメータ**:
- `format`: `txt`, `srt`, `json` のいずれか

**応答**: ファイルダウンロード（Content-Disposition: attachment）

---

### 4.2 セグメント編集

#### `PATCH /api/segments/{id}`

**用途**: 1つのセグメントを更新（テキスト編集 or 個別話者名上書き）

**リクエスト**:
```json
{
  "text": "修正後のテキスト",         // オプション
  "display_name": "鈴木さん"            // オプション、null で個別上書き解除
}
```

**応答**: `200 OK`
```json
{
  "id": 4567,
  "text": "修正後のテキスト",
  "display_name": "鈴木さん",
  "is_edited": true,
  "updated_at": "..."
}
```

**権限**: 親 transcript の所有者のみ

---

### 4.3 話者管理（F-1 対応）

#### `PATCH /api/transcripts/{id}/speakers/{label}`

**用途**: 内部ラベル（`SPEAKER_00`）に対する表示名を一括変更

**リクエスト**:
```json
{
  "display_name": "山田さん"
}
```

**応答**: `200 OK`
```json
{
  "speaker_label": "SPEAKER_00",
  "display_name": "山田さん",
  "color": "#3B82F6",
  "affected_segments": 23
}
```

**動作**:
1. `speakers` テーブルに UPSERT
2. 同 transcript の `segments` で個別上書き（`display_name`）されているものはそのまま残す
3. `speaker_history` を更新（last_used_at, use_count）

#### `POST /api/transcripts/{id}/speakers/merge`

**用途**: F-1 の「すべての XXX に適用」相当。表示名ベースで複数話者をマージ

**リクエスト**:
```json
{
  "from_display_name": "未設定話者2",
  "to_display_name": "山田さん"
}
```

**応答**: `200 OK`
```json
{
  "merged_speaker_count": 2,
  "affected_segments": 45
}
```

**動作**: 表示名が `from_display_name` のすべての話者を `to_display_name` に変更する。複数の内部ラベルを横断するマージが可能。

---

### 4.4 話者履歴（Notta 風候補リスト）

#### `GET /api/speaker-history`

**用途**: 話者編集ドロップダウンの候補リスト取得

**クエリパラメータ**:
- `q`: 検索クエリ（前方一致、オプション）
- `limit`: 取得件数（デフォルト 20）

**応答**: `200 OK`
```json
{
  "items": [
    {"name": "山田さん", "last_used_at": "...", "use_count": 12},
    {"name": "鈴木さん", "last_used_at": "...", "use_count": 8},
    ...
  ]
}
```

**並び順**: `last_used_at DESC`（最近使った順）

---

### 4.5 インポート（D-1 対応）

#### `POST /api/transcripts/import`

**用途**: 旧 Mac アプリ版の `.transcription.json` をインポート

**リクエスト**: `multipart/form-data`
- `file`: `.transcription.json` ファイル

**応答**: `201 Created`
```json
{
  "id": 456,
  "imported_segments": 234,
  "imported_speakers": 4
}
```

---

## 5. システム関連

### `GET /health`

**用途**: Railway のヘルスチェック用

**応答**: `200 OK`
```json
{"status": "ok"}
```

**認証**: 不要

---

## 6. エラーフォーマット

すべての JSON API は以下の統一フォーマットでエラーを返す:

```json
{
  "error": {
    "code": "INVALID_FILE_FORMAT",
    "message": "対応していないファイル形式です。mp3 か mp4 をご利用ください。",
    "details": {
      "received_extension": ".aac"
    }
  }
}
```

### エラーコード一覧（抜粋）

| コード | HTTP ステータス | ユーザー向けメッセージ |
|---|---|---|
| `NOT_AUTHENTICATED` | 401 | ログインしてください |
| `FORBIDDEN_DOMAIN` | 403 | 社内アカウントでログインしてください |
| `NOT_FOUND` | 404 | 該当するデータが見つかりません |
| `INVALID_FILE_FORMAT` | 415 | mp3 か mp4 をご利用ください |
| `FILE_TOO_LARGE` | 413 | ファイルサイズが 2GB を超えています |
| `FILE_TOO_LONG` | 400 | 3時間を超える音声には確認が必要です |
| `RATE_LIMITED` | 429 | アクセスが集中しています。しばらく待ってください |
| `ASSEMBLYAI_ERROR` | 502 | 文字起こしサービスでエラーが発生しました |
| `BUDGET_EXCEEDED` | 503 | 今月の利用上限に達しました。管理者にご連絡ください |
| `INTERNAL_ERROR` | 500 | 内部エラーが発生しました |

---

## 7. ステータスコード

| コード | 用途 |
|---|---|
| `200 OK` | 正常取得 |
| `201 Created` | 新規作成成功 |
| `204 No Content` | 削除成功（応答ボディなし） |
| `301 / 302` | リダイレクト |
| `400 Bad Request` | リクエスト形式が不正 |
| `401 Unauthorized` | 認証されていない |
| `403 Forbidden` | 権限がない |
| `404 Not Found` | リソースが存在しない |
| `413 Payload Too Large` | ファイルが大きすぎる |
| `415 Unsupported Media Type` | 非対応のファイル形式 |
| `422 Unprocessable Entity` | バリデーションエラー |
| `429 Too Many Requests` | レート制限超過 |
| `500 Internal Server Error` | サーバー内部エラー |
| `502 Bad Gateway` | 外部サービス（AssemblyAI）エラー |
| `503 Service Unavailable` | サービス停止中 |

---

## 8. API バージョニング

### 方針
- 初期実装では **バージョニングしない**（`/api/v1` のような prefix なし）
- 互換性破壊の変更が必要になった時点で `/api/v2` を導入
- 内部利用のみなので、外部 API 公開のような厳密性は不要

### 互換性が壊れる変更の例
- 必須フィールドの追加
- フィールド名の変更
- フィールドの型変更
- 既存エンドポイントの削除

これらが必要になったら、別バージョンとして並走させる。

---

## 9. OpenAPI ドキュメントの自動生成

FastAPI を使うので、コードから API 仕様が自動生成される：

- **`GET /docs`** — Swagger UI（インタラクティブ）
- **`GET /redoc`** — ReDoc（読み物形式）
- **`GET /openapi.json`** — OpenAPI 3 仕様書（機械可読）

これらは **コードから自動生成** されるので、実装と仕様がズレない。

### 本ファイルとの関係

- `API.md`（本ファイル）: **設計意図と全体像**を人間向けに記述
- `/docs`（自動生成）: **実装の正確な仕様**を機械的に記述

両方を維持する。設計が変わったら本ファイルを更新、実装が変わったら自動生成が追従。

---

## 10. 実装フェーズでの確認ポイント

実装が進んだら、以下を再確認する：

- [ ] すべてのエンドポイントに認証が掛かっているか（`/health` 等の例外を除く）
- [ ] 権限チェック（自分の transcript しかアクセスできない）
- [ ] エラーフォーマットの統一
- [ ] 大容量ファイルのストリーミング処理（メモリに全部読み込まない）
- [ ] CSRF 対策（同一オリジン + SameSite Cookie + 状態変更系は要トークン検討）
- [ ] レート制限（v1 では未実装、ただし設計は意識）
- [ ] ログ記録（誰がいつ何にアクセスしたか）
