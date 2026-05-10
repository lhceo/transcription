# Noto.app ビルド手順

社内 Mac へ配布するための `.app` パッケージを作る手順です。Apple Silicon (M1/M2/M3/M4) macOS 12+ 専用。

## 必要なもの

- Apple Silicon Mac
- python.org 配布版の Python 3.12 を入れた venv（`~/transcription/venv`）
  - 詳細はプロジェクトの履歴参照（Tk 8.6 が必要なので Homebrew Python ではダメ）
- 約 10 GB の空きディスク（ビルド中に一時的に使用）

## ビルド

リポジトリのルートで：

```bash
bash scripts/build_app.sh
```

これで以下が自動実行されます：

1. `bundled/ffmpeg/ffmpeg` が無ければ evermeet.cx から static binary をダウンロード
2. venv に `py2app` が無ければインストール
3. 前回の `build/` `dist/` を削除
4. `python setup.py py2app` 実行
5. 結果は `dist/Noto.app`

ビルド時間：初回 5〜10 分、依存をキャッシュした 2 回目以降は 2〜3 分。

完了後、Finder から `dist/Noto.app` をダブルクリックすると起動確認できます。
ターミナルからは `open dist/Noto.app` でも OK。

## 配布先 Mac へのインストール

1. `dist/Noto.app` を ZIP で固める：`ditto -c -k --keepParent dist/Noto.app dist/Noto.app.zip`
2. ZIP を社内ユーザーに渡す
3. ユーザーは ZIP を解凍 → `Noto.app` を `/Applications` にドラッグ

### 初回起動時の注意

未署名アプリのため、初回起動でこの警告が出ます：

> "Noto" は Apple によって悪質なソフトウェアであるかどうかが
> 確認できないため、開けません。

回避手順：

1. Finder で `Noto.app` を **右クリック → 開く**
2. ダイアログで **開く** ボタンを押す（一度だけでOK、以降は普通にダブルクリック起動可）

### 必要な事前設定

ユーザーに事前にやってもらうこと：

- HuggingFace アカウント作成 → Read 権限の Token を発行
- 以下2つのモデル利用規約に同意：
  - https://huggingface.co/pyannote/speaker-diarization-3.1
  - https://huggingface.co/pyannote/segmentation-3.0
- アプリの設定画面で Token を入力
- インターネット接続を確保（初回のみ。文字起こしモデル ~3GB と話者分離モデル ~30MB をダウンロード）

## トラブルシューティング

### ビルドが失敗する

- `py2app` のバージョン更新で挙動が変わることがあります。`pip install --upgrade py2app` で更新
- 大量の警告が出ますが、最後に `dist/Noto.app` ができていれば成功
- mlx-whisper / pyannote が見つからない系のエラーは `setup.py` の `packages` / `includes` を調整

### .app が起動しない

- ターミナルから直接実行してエラーを確認：`./dist/Noto.app/Contents/MacOS/Noto`
- 多くの場合、`packages` に未登録のサブモジュールが原因。表示される `ModuleNotFoundError` を `setup.py` の `includes` に追加

### .app が大きすぎる

- 期待値は 1〜3 GB（PyTorch + MLX + Pillow + Whisper モデル管理コード）
- HuggingFace モデルキャッシュは `.app` には含めず、初回起動時にユーザーホームへダウンロード
