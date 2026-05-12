# システムアーキテクチャ（ARCHITECTURE.md）

このファイルは Web アプリ版のシステム構成を視覚化したものです。  
コードに入る前に「どう動くか」の地図として参照します。

最終更新: 2026-05-12

---

## 1. システム全体図

```mermaid
graph LR
  User[👤 ユーザー<br/>ブラウザ]

  subgraph Railway[Railway<br/>マネージドホスティング]
    App[🐍 Web アプリ<br/>FastAPI]
    DB[(💾 SQLite<br/>永続ボリューム)]
    Files[📁 一時ファイル<br/>/tmp/uploads]
  end

  Google[🔐 Google OAuth<br/>Google Workspace]
  Assembly[🎙 AssemblyAI<br/>文字起こしサービス]

  User <-->|HTTPS| App
  App <--> DB
  App <--> Files
  App <-->|ログイン認証| Google
  App <-->|音声アップロード<br/>結果取得| Assembly
```

### コンポーネントの責務

| コンポーネント | 役割 |
|---|---|
| **ブラウザ（ユーザー）** | UI 表示、ファイル選択、結果閲覧・編集 |
| **Web アプリ（FastAPI）** | リクエスト処理、認証、AssemblyAI 連携、データ永続化 |
| **SQLite DB** | ユーザー情報、ジョブ状態、文字起こし結果、話者名履歴を保存 |
| **一時ファイル領域** | アップロード中の音声ファイルを一時保存（処理後即削除） |
| **Google OAuth** | ユーザー認証（社内ドメイン限定） |
| **AssemblyAI** | 文字起こし＋話者分離の実処理 |

---

## 2. 認証フロー（Google OAuth）

```mermaid
sequenceDiagram
  participant U as ユーザー<br/>ブラウザ
  participant A as Web アプリ<br/>(FastAPI)
  participant G as Google OAuth

  U->>A: アプリ URL にアクセス
  A->>A: セッション確認
  alt 未ログイン
    A-->>U: ログイン画面表示
    U->>A: 「Google でログイン」クリック
    A-->>U: Google 認証ページにリダイレクト
    U->>G: Google アカウントで認証
    G-->>U: 認可コード付きでアプリに戻る
    U->>A: 認可コード送信
    A->>G: コードを ID トークンに交換
    G-->>A: ID トークン + ユーザー情報
    A->>A: メールドメイン検証<br/>（@lionheart.co.jp 等のみ許可）
    A->>A: DB にユーザー登録 or 取得
    A->>A: セッション Cookie 発行
    A-->>U: ホーム画面にリダイレクト
  else ログイン済み
    A-->>U: ホーム画面を表示
  end
```

### 認証の重要な決定事項

- **ドメイン制限**: ID トークンの `email` フィールドを検証し、許可ドメイン以外は弾く
- **セッション管理**: HttpOnly Cookie + Secure 属性（HTTPS 必須）
- **退職者対応**: Google Workspace 側でアカウント停止すれば、即座にログイン不可になる
- **ログアウト**: セッション Cookie を削除（Google 側のログアウトは強制しない）

---

## 3. 文字起こし処理フロー（非同期）

```mermaid
sequenceDiagram
  participant U as ユーザー<br/>ブラウザ
  participant A as Web アプリ
  participant T as バックグラウンド<br/>タスク
  participant Aai as AssemblyAI
  participant DB as SQLite DB

  U->>A: 音声ファイルをアップロード
  A->>A: ファイル検証<br/>(サイズ・形式)
  A->>A: /tmp に一時保存
  A->>DB: ジョブ作成<br/>(status: uploading)
  A->>Aai: 音声ファイルを送信
  Aai-->>A: transcript_id 返却
  A->>DB: ジョブ更新<br/>(status: processing, transcript_id)
  A->>A: ローカルの音声ファイル削除
  A->>T: バックグラウンドポーリング開始
  A-->>U: 「処理中」ページ表示<br/>(ジョブIDをブラウザにも保存)

  Note over U,A: ここでユーザーは<br/>ブラウザを閉じてもOK

  loop 数秒ごと（処理完了まで）
    T->>Aai: ステータス確認
    Aai-->>T: 進行状況
  end

  Aai-->>T: 処理完了
  T->>Aai: 結果取得
  Aai-->>T: 文字起こし結果
  T->>DB: 結果保存<br/>(segments テーブル)
  T->>Aai: 削除依頼
  T->>DB: ジョブ更新<br/>(status: completed)

  U->>A: 結果を見に来る<br/>(ジョブID参照)
  A->>DB: 結果取得
  A-->>U: 文字起こし結果表示
```

### 重要な設計ポイント

1. **完全非同期（E-1 方針）**  
   ユーザーがブラウザを閉じても、バックグラウンドタスクが処理を継続する。  
   AssemblyAI 側で結果が完成してから、ユーザーがブラウザを開き直しても結果を取得できる。

2. **ファイル保持の最小化（A-4 方針）**  
   音声ファイルは：
   - 自社サーバー（/tmp）: AssemblyAI に送り終わったら即削除
   - AssemblyAI: 処理完了後に即削除依頼
   - 永続保存される音声データはどこにもない

3. **結果はサーバー DB に保存**  
   文字起こしの「結果（テキスト＋話者）」のみが SQLite に永続化される。  
   ユーザーはいつでも、どのブラウザからでも結果を見られる。

4. **バックグラウンドタスクの実現方式**  
   FastAPI の `BackgroundTasks` または `asyncio.create_task` を使用。  
   Celery のようなジョブキューは不要（規模が小さいため）。  
   サーバー再起動時に未完了ジョブをどう扱うかは要設計（DB に状態保存しておけば再開可能）。

---

## 4. データの所在マップ

「いつ・どこに・どのデータがあるか」を整理：

```mermaid
graph TD
  subgraph 処理前
    A[音声ファイル<br/>📁 ユーザー Mac]
  end

  subgraph アップロード中
    B[音声ファイル<br/>📁 自社サーバー /tmp]
    C[ジョブ記録<br/>💾 SQLite<br/>status: uploading]
  end

  subgraph 処理中
    D[音声ファイル<br/>🎙 AssemblyAI 一時保管]
    E[ジョブ記録<br/>💾 SQLite<br/>status: processing]
  end

  subgraph 処理完了後
    F[文字起こし結果<br/>💾 SQLite<br/>segments テーブル]
    G[音声ファイル<br/>❌ どこにも残さない]
  end

  A -->|アップロード| B
  B -->|送信後 即削除| D
  D -->|処理完了後 削除依頼| G
  C --> E --> F
```

### プライバシー保証

- 音声ファイル: **処理完了後 24時間以内** にすべてのサーバーから消える
- AssemblyAI への削除依頼が失敗した場合のフェイルセーフを実装する（再試行ロジック）
- 結果テキストは DB に残るが、ユーザーが削除すれば即消去

---

## 5. デプロイ構成（Railway）

```mermaid
graph TB
  subgraph Railway Project
    subgraph Service[Web Service]
      App[FastAPI アプリ<br/>Python 3.12]
      Static[静的ファイル<br/>HTMX + Alpine.js]
    end

    Volume[💾 永続ボリューム<br/>SQLite + /tmp]

    Env[🔐 環境変数<br/>API キー・OAuth 設定]

    App --> Volume
    App -.-> Env
  end

  GitHub[📦 GitHub<br/>web-app-rewrite ブランチ]
  CDN[🌐 Railway 提供 HTTPS]
  User[👤 ユーザー]

  GitHub -->|Git push で自動デプロイ| Service
  User --> CDN --> Service
```

### Railway 上での運用

- **デプロイ**: GitHub の `web-app-rewrite` ブランチに push すれば自動でビルド＆デプロイ
- **環境変数**: AssemblyAI API キー、Google OAuth 設定、セッションシークレットなどを管理画面で設定
- **永続ボリューム**: SQLite ファイルと一時ファイル領域を保持（再デプロイで消えない）
- **HTTPS**: Railway が自動で SSL 証明書を発行・更新（手動作業不要）
- **ドメイン**: 初期は `*.railway.app` の自動ドメイン、必要なら独自ドメイン設定可能
- **ログ・監視**: 管理画面でリアルタイムログが見られる

---

## 6. ファイルレベルの構成（コードの整理）

実装時のディレクトリ構成（暫定）：

```
transcription/
├── CLAUDE.md
├── docs/                       ← 設計ドキュメント群
│   ├── ARCHITECTURE.md
│   ├── DESIGN.md
│   ├── DECISIONS.md
│   ├── ROADMAP.md
│   ├── CHANGELOG.md
│   ├── RISKS.md
│   ├── DATA_MODEL.md           ← 次のセッションで作成
│   ├── API.md                   ← 次のセッションで作成
│   └── SECURITY.md              ← 次のセッションで作成
├── backend/                    ← FastAPI のコード
│   ├── main.py                 ← FastAPI エントリポイント
│   ├── config.py               ← 設定読み込み（環境変数）
│   ├── auth/                   ← Google OAuth 関連
│   │   ├── routes.py
│   │   └── google.py
│   ├── transcribe/             ← 文字起こし関連
│   │   ├── routes.py
│   │   ├── assemblyai_client.py
│   │   └── tasks.py            ← バックグラウンドタスク
│   ├── db/                     ← データベース層
│   │   ├── models.py           ← SQLAlchemy モデル
│   │   ├── session.py
│   │   └── migrations/
│   └── static/                 ← フロントエンド静的ファイル
│       ├── index.html
│       ├── css/
│       └── js/
├── tests/                      ← テスト
└── pyproject.toml              ← Python 依存関係定義
```

### 設計上の原則

- **1ファイル 500行以内** を目安に分割（旧 app.py の 2,191行 への反省）
- **機能ごとにフォルダ**（auth/, transcribe/, db/）
- **ルーティング・ロジック・DB アクセスを分離**

---

## 7. このアーキテクチャの根拠

### なぜこの構成？

| 設計判断 | 根拠 |
|---|---|
| Web アプリ化 | 場所に縛られない作業を実現するため（DECISIONS.md 参照） |
| FastAPI | 非同期処理・型安全性・Claude Code との相性 |
| SQLite | 5〜8人規模では十分。運用がシンプル |
| HTMX + Alpine | SPA は過剰。ビルドツール不要 |
| Railway | デプロイの容易さ、Git 連携、永続ボリューム |
| Google OAuth | 既に Google Workspace を使用、退職者対応も統合 |
| 非同期処理（E-1） | 中断時にも結果を失わない、Notta 的メンタルモデル |

### このアーキテクチャの限界（将来課題）

- **垂直スケール限界**: SQLite の writer lock により、100人を超える同時利用は要再設計
- **マルチリージョン**: Railway は単一リージョン運用が前提
- **大規模ファイル**: 2GB を超える音声は別アプローチが必要
- **オフライン対応**: 設計上不可（必要なら別アーキテクチャ）

これらの限界に当たったら、Postgres + 別ホスティング + ジョブキューを検討する。
