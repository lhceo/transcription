# 音声文字起こし Web アプリ

社内向け音声文字起こしツール。録音した会議・打ち合わせの音声を AssemblyAI で文字起こしし、ブラウザから話者付きで結果を確認・編集できる。

> 詳細な設計は [`docs/`](./docs/) 配下を参照。新しいセッションを開いた Claude は [`CLAUDE.md`](./CLAUDE.md) を最初に読んでください。

---

## 現在のステータス（2026-05-12）

**フェーズ: Phase 1 / Web スケルトン**

- ✅ FastAPI が起動
- ✅ Jinja2 テンプレート + 静的ファイル
- ✅ HTMX + Alpine.js 読み込み
- ⏳ 認証・アップロード・AssemblyAI 連携は後続フェーズ

---

## 開発環境のセットアップ（初回のみ）

### 1. Python 3.12 を用意

```bash
python3 --version  # 3.12 以上であることを確認
```

不足していれば [python.org](https://www.python.org/downloads/) からインストール（または Homebrew で `brew install python@3.12`）。

### 2. このリポジトリをクローン

```bash
git clone <repo-url>
cd transcription
```

### 3. 仮想環境（venv）を作成

```bash
python3 -m venv .venv
```

### 4. 仮想環境を有効化

```bash
source .venv/bin/activate
```

ターミナルの先頭に `(.venv)` が表示されればOK。

### 5. 依存関係をインストール

```bash
pip install --upgrade pip
pip install -e ".[dev]"
```

### 6. 環境変数ファイルを準備

```bash
cp .env.example .env
```

`.env` の中身は Phase 1 時点ではそのままで OK。Phase 2 以降で値を埋めていく。

---

## アプリの起動

```bash
source .venv/bin/activate  # まだ有効化していない場合
uvicorn backend.main:app --reload
```

ブラウザで `http://127.0.0.1:8000` を開くと、Phase 1 のホーム画面が表示される。

### 動作確認ポイント

ホーム画面で以下が確認できれば Phase 1 は完了：
- ヘッダーに「音声文字起こし」と表示される
- 「🚧 開発中：Phase 1（Web スケルトン）」のメッセージが見える
- 「ヘルスチェック実行」ボタンを押すと、HTMX 経由で `/health` を呼び、応答が画面に表示される

### 別ポートで起動したい場合

```bash
uvicorn backend.main:app --reload --port 8080
```

### 起動を止める

ターミナルで `Ctrl + C`。

---

## プロジェクト構造

```
transcription/
├── CLAUDE.md              ← Claude 用の道しるべ
├── README.md              ← このファイル
├── pyproject.toml         ← Python 依存関係定義
├── .env.example           ← 環境変数のテンプレート
├── docs/                  ← 設計ドキュメント群
│   ├── DESIGN.md
│   ├── ARCHITECTURE.md
│   ├── DATA_MODEL.md
│   ├── API.md
│   ├── SECURITY.md
│   └── ...
├── backend/               ← FastAPI のコード
│   ├── main.py            ← エントリポイント
│   ├── config.py          ← 設定読み込み
│   ├── auth/              ← 認証関連（Phase 2）
│   ├── transcribe/        ← 文字起こし関連（Phase 4）
│   ├── db/                ← データベース層（Phase 3）
│   ├── templates/         ← Jinja2 テンプレート
│   └── static/            ← CSS/JS
└── tests/                 ← テスト
```

---

## トラブルシューティング

### `uvicorn` コマンドが見つからない
- 仮想環境を有効化していない可能性。`source .venv/bin/activate` を実行。

### `ModuleNotFoundError: No module named 'backend'`
- リポジトリのルート（`pyproject.toml` がある場所）で `uvicorn backend.main:app --reload` を実行しているか確認。
- `pip install -e ".[dev]"` を再実行。

### ポート 8000 が既に使われている
- 他のアプリが使用中。`--port 8080` などで別ポートを使う。

### `.venv` を作り直したい
- 仮想環境を抜けて削除して作り直す:
  ```bash
  deactivate
  rm -rf .venv
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -e ".[dev]"
  ```

---

## 参考リンク

- [FastAPI 公式](https://fastapi.tiangolo.com/)
- [HTMX 公式](https://htmx.org/)
- [Alpine.js 公式](https://alpinejs.dev/)
- [AssemblyAI](https://www.assemblyai.com/)（Phase 4 で利用）
