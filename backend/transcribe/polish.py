"""整文（文字起こし校正）機能。

Claude API を使って粗い音声認識テキストを正確に整える。
要約・省略・補完は一切行わず、フィラー除去・誤認識修正・表記統一のみ実施する。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# モデル定義 ─────────────────────────────────────────────────────────────────
MODELS = {
    "haiku": {
        "id": "claude-haiku-4-5-20251001",
        "label": "Haiku（標準・低コスト）",
        "input_price_per_mtok": 0.80,   # USD per million tokens
        "output_price_per_mtok": 4.00,
    },
    "sonnet": {
        "id": "claude-sonnet-4-6",
        "label": "Sonnet（高精度・高コスト）",
        "input_price_per_mtok": 3.00,
        "output_price_per_mtok": 15.00,
    },
}

USD_TO_JPY = 150  # 概算レート


# コスト計算 ──────────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """日本語テキストのトークン数を概算する（1文字≒0.7トークン）。"""
    return max(1, int(len(text) * 0.7))


def estimate_cost(input_tokens: int, output_tokens: int, model_key: str) -> float:
    """予想コスト（円）を返す。"""
    m = MODELS.get(model_key, MODELS["haiku"])
    usd = (
        input_tokens / 1_000_000 * m["input_price_per_mtok"]
        + output_tokens / 1_000_000 * m["output_price_per_mtok"]
    )
    return round(usd * USD_TO_JPY, 1)


def actual_cost_yen(input_tokens: int, output_tokens: int, model_key: str) -> float:
    return estimate_cost(input_tokens, output_tokens, model_key)


# コンテキスト構築 ─────────────────────────────────────────────────────────────

def build_context_text(
    *,
    project_name: str | None = None,
    project_description: str | None = None,
    members: list[dict] | None = None,
    vocabulary: list[dict] | None = None,
    project_attachments_summaries: list[str] | None = None,
    meeting_date: str | None = None,
    meeting_location: str | None = None,
    meeting_overview: str | None = None,
    meeting_purpose: str | None = None,
    meeting_agenda: str | None = None,
    mtg_attachments_summaries: list[str] | None = None,
) -> str:
    """整文プロンプトに埋め込むコンテキストブロックを構築する。"""
    lines: list[str] = []

    if project_name:
        lines.append(f"【プロジェクト名】{project_name}")
    if project_description:
        lines.append(f"【プロジェクト概要】\n{project_description}")

    if members:
        lines.append("【参加者・メンバー】")
        for m in members:
            role = m.get("project_role") or ""
            company = m.get("company") or ""
            parts = [m.get("display_name") or "（名前なし）"]
            if company:
                parts.append(company)
            if role:
                parts.append(role)
            lines.append("  - " + " / ".join(parts))

    if vocabulary:
        lines.append("【固有名詞辞書】")
        for v in vocabulary:
            word = v.get("word", "")
            meaning = v.get("meaning") or ""
            if meaning:
                lines.append(f"  - {word}: {meaning}")
            else:
                lines.append(f"  - {word}")

    if project_attachments_summaries:
        lines.append("【PJT添付資料（要約）】")
        for s in project_attachments_summaries:
            lines.append(s)

    # MTG情報
    mtg_parts: list[str] = []
    if meeting_date:
        mtg_parts.append(f"日時: {meeting_date}")
    if meeting_location:
        mtg_parts.append(f"場所: {meeting_location}")
    if meeting_overview:
        mtg_parts.append(f"概要:\n{meeting_overview}")
    if meeting_purpose:
        mtg_parts.append(f"目的: {meeting_purpose}")
    if meeting_agenda:
        mtg_parts.append(f"アジェンダ:\n{meeting_agenda}")
    if mtg_parts:
        lines.append("【MTG情報】")
        lines.extend(mtg_parts)

    if mtg_attachments_summaries:
        lines.append("【MTG添付資料（要約）】")
        for s in mtg_attachments_summaries:
            lines.append(s)

    return "\n".join(lines)


# 固有名詞ピックアップ ─────────────────────────────────────────────────────────

PICKUP_SYSTEM = """あなたは日本語の文字起こしテキストを分析するアシスタントです。
テキストから固有名詞・専門用語・意味が不明確な語句を抽出してください。

条件:
- 人名・会社名・製品名・プロジェクト名・略称・ニックネームなどを対象にする
- 既に辞書に登録済みの語句は除外する
- 一般的な日本語の単語は除外する
- 最大15件まで

必ずJSONで返してください:
{"words": ["単語1", "単語2", ...]}"""


async def pickup_proper_nouns(
    client,
    transcript_text: str,
    registered_words: list[str],
) -> list[str]:
    """文字起こしテキストから未登録の固有名詞候補を抽出する。"""
    registered_str = "、".join(registered_words) if registered_words else "（なし）"
    # 長い音声でも後半の用語を取りこぼさないよう先頭・中間・末尾からサンプリング
    n = len(transcript_text)
    if n <= 3000:
        sample = transcript_text
    else:
        chunk = 1000
        mid = n // 2
        sample = (
            transcript_text[:chunk] + "\n…\n" +
            transcript_text[mid - chunk // 2: mid + chunk // 2] + "\n…\n" +
            transcript_text[-chunk:]
        )
    prompt = f"""以下の文字起こしテキストから固有名詞・専門用語を抽出してください。
登録済みの語句は除外してください: {registered_str}

【文字起こし】
{sample}"""

    try:
        message = await client.messages.create(
            model=MODELS["haiku"]["id"],  # ピックアップは常にHaikuで十分
            max_tokens=512,
            system=PICKUP_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        data = json.loads(text)
        return data.get("words", [])
    except Exception as e:
        logger.warning("固有名詞ピックアップ失敗: %s", e)
        return []


# 整文実行 ────────────────────────────────────────────────────────────────────

POLISH_SYSTEM = """あなたは日本語のMTG文字起こし整文アシスタントです。
目的は「MTGに参加していない人が読んでも、やり取りが完全に理解できる文書にすること」です。
整文後のテキストは議事録・要件定義書の作成や、AIへのコンテキスト入力として使用されます。

【絶対に守るルール】
- 発言の意味・内容・順序は変えない
- 要約・省略・内容の創作は一切しない
- 主語・目的語の補完は「文脈から確実に推察できる場合のみ」行う

【整文の手順】

ステップ1: フィラー除去
えー・あのー・うーん・まあ・えっと・なんか（間投詞として使われている場合）等を除去する。
ただし「まあ、そうですね」のように意味のある使い方はそのまま残す。

ステップ2: 音声認識の誤認識修正
固有名詞辞書・前後の文脈をもとに、明らかな誤認識を修正する。
辞書に登録済みの語句（例: 「のーと」→「Noto」）を優先的に確認する。

ステップ3: 句読点・表記の統一
句読点の欠落を補い、敬体（です・ます）か常体かのブレを統一する。

ステップ4: 「誰が・誰に・何を」の明確化（最重要）
日本語の会話では主語・目的語・間接目的語が頻繁に省略される。
文字起こしをコンテキストとして活用するには「誰が・誰に・何を」を明示することが不可欠。
以下のルールで補完する:

【ルールA】省略された主語・目的語・間接目的語を補完できる場合
→ 全角括弧 （） で囲み、文中の自然な位置に挿入する
  発言: 「対応したから問題は納まった」
  整文: 「（Aさんが）対応したから問題は納まった」

  発言: 「送っておいて」
  整文: 「（見積書をBさんに）送っておいて」
  ※ 複数の補完は （） 一つにまとめる

  発言: 「名古屋に戻って弊社に来た」
  整文: 「（古瀬社長が）名古屋に戻って弊社に来た」

  ※ 全角括弧 （） = 「発言にはなかったが文脈から補った」という慣習的マーカー

【ルールB】指示語（これ・それ・あれ・ここ・そこ・あの件・先日の・例のやつ 等）
→ 文脈から特定できる場合、具体的な語句に置き換える
  発言: 「それを進めておいて」
  整文: 「ランディングページの原稿を進めておいて」

【ルールC】推察できない・確信が持てない場合
→ 元の表現を残し （？） を挿入する
  例: 「（？）対応してもらえますか」
  例: 「あの件（？）については次回確認します」
  ※ （？） は「参加者以外には判断できない箇所」のマーカー

補完の優先根拠:
  1. 直前・直後の発言の流れ
  2. 話者の役職・会社・プロジェクト役割（speakerフィールド参照）
  3. MTGのアジェンダ・目的・概要
  4. 固有名詞辞書

ステップ5: 話者属性の活用
speakerに含まれる会社・役職・役割の情報を推察の補強に使う。
例: クライアント企業の意思決定者の「それで問題ありません」は承認として解釈できる。
例: 受注者側のPMの「確認します」は社内確認のアクションとして解釈できる。

ステップ6: 判断できない箇所は原文のまま残す（推測で内容を書き換えない）

ステップ7: 各セグメントを必ず同じJSON形式で返す"""

POLISH_USER_TEMPLATE = """{context}

【文字起こし（JSON形式）】
※ commentフィールドがある場合、それはユーザーが記録した補足メモです。整文の文脈理解に活用してください。
{segments_json}

上記の文字起こしを整文してください。
speakerフィールドの話者名・会社・役職・役割をコンテキスト推察に積極的に活用してください。
「誰が・誰に・何を」を最優先で明確化し、省略された主語・目的語は （補完内容） 形式で文中に挿入してください。
指示語（これ・それ・あれ等）は具体的な語句に置き換えてください。
推察できない箇所のみ （？） を挿入してください。

必ず以下のJSON形式のみで返してください（前後に説明文を付けないこと。セグメント数・idは変えないこと）:
{{"segments": [{{"id": <id>, "text": "<整文後のテキスト>"}}]}}"""


# 出力トークン閾値: これを超えると分割処理に切り替える
# 8192 制限に対して余裕を持たせた安全値（注釈追加で若干膨張するため）
_SAFE_OUTPUT_TOKENS = 7000
# バッチ間のオーバーラップ（文脈保持のため前バッチ末尾を次バッチ先頭に重複させる）
_OVERLAP_SEGMENTS = 5


@dataclass
class PolishResult:
    suggestions: dict[int, str]  # segment_id -> 整文後テキスト
    input_tokens: int
    output_tokens: int
    cost_yen: float


async def _run_polish_batch(
    client,
    segments: list[dict],
    context_text: str,
    model_id: str,
) -> tuple[dict[int, str], int, int]:
    """セグメントのリストを1回のAPI呼び出しで整文し、(suggestions, input_tokens, output_tokens) を返す。"""
    segments_json = json.dumps(
        [
            {
                "id": s["id"],
                "speaker": s.get("speaker", ""),
                "text": s["text"],
                **({"comment": s["comment"]} if s.get("comment") else {}),
            }
            for s in segments
        ],
        ensure_ascii=False,
        indent=2,
    )
    prompt = POLISH_USER_TEMPLATE.format(
        context=context_text if context_text else "（コンテキスト情報なし）",
        segments_json=segments_json,
    )
    message = await client.messages.create(
        model=model_id,
        max_tokens=8192,
        system=POLISH_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        json_str = m.group(0) if m else raw
        data = json.loads(json_str)
        suggestions = {s["id"]: s["text"] for s in data.get("segments", [])}
    except Exception:
        logger.error("整文レスポンスのJSONパース失敗: %s", raw[:500])
        suggestions = {}
    return suggestions, message.usage.input_tokens, message.usage.output_tokens


async def run_polish(
    client,
    segments: list[dict],
    context_text: str,
    model_key: str = "haiku",
) -> PolishResult:
    """整文を実行し、セグメントごとの提案テキストを返す。

    出力トークンが _SAFE_OUTPUT_TOKENS 以内なら一括送信。
    超えそうな場合は必要最小限のバッチ数に分割し、バッチ間は
    _OVERLAP_SEGMENTS 件をオーバーラップさせて文脈を保持する。

    Args:
        client: anthropic.AsyncAnthropic インスタンス
        segments: [{"id": int, "text": str}, ...]
        context_text: build_context_text() で構築したコンテキスト
        model_key: "haiku" | "sonnet"
    """
    model_id = MODELS.get(model_key, MODELS["haiku"])["id"]

    # 出力トークンを概算（日本語 0.7 tok/char × 注釈膨張 1.2 倍）
    estimated_output = int(sum(estimate_tokens(s["text"]) for s in segments) * 1.2)
    logger.info("整文 出力トークン概算: %s segs → %s tok (threshold %s)", len(segments), estimated_output, _SAFE_OUTPUT_TOKENS)

    total_input = 0
    total_output = 0
    merged: dict[int, str] = {}

    if estimated_output <= _SAFE_OUTPUT_TOKENS:
        # 一括送信
        suggestions, inp, out = await _run_polish_batch(client, segments, context_text, model_id)
        merged.update(suggestions)
        total_input += inp
        total_output += out
    else:
        # 必要最小限のバッチ数に分割
        n_batches = math.ceil(estimated_output / _SAFE_OUTPUT_TOKENS)
        batch_size = math.ceil(len(segments) / n_batches)
        logger.info("整文 分割処理: %s バッチ (各 %s セグメント + オーバーラップ %s)", n_batches, batch_size, _OVERLAP_SEGMENTS)

        for i in range(n_batches):
            own_start = i * batch_size
            own_end = min(len(segments), (i + 1) * batch_size)
            # オーバーラップ: 前バッチ末尾を先頭に重複させて文脈を保持
            fetch_start = max(0, own_start - _OVERLAP_SEGMENTS)
            batch_segs = segments[fetch_start:own_end]

            suggestions, inp, out = await _run_polish_batch(client, batch_segs, context_text, model_id)

            # オーバーラップ部分（前バッチが担当）は採用しない
            own_ids = {s["id"] for s in segments[own_start:own_end]}
            for seg_id, text in suggestions.items():
                if seg_id in own_ids:
                    merged[seg_id] = text

            total_input += inp
            total_output += out

    cost = actual_cost_yen(total_input, total_output, model_key)
    return PolishResult(
        suggestions=merged,
        input_tokens=total_input,
        output_tokens=total_output,
        cost_yen=cost,
    )
