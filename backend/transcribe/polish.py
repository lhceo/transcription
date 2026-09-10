"""整文（文字起こし校正）機能。

Claude API を使って粗い音声認識テキストを正確に整える。
要約・省略・補完は一切行わず、フィラー除去・誤認識修正・表記統一のみ実施する。
"""

from __future__ import annotations

import json
import logging
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

POLISH_SYSTEM = """あなたは日本語の文字起こし校正アシスタントです。
粗い音声認識テキストを正確に整えてください。
整形後のテキストは議事録や要件定義書への貼り付けを想定しています。

ルール:
1. フィラー（えー、あのー、うーん、まあ、えっと 等）を除去する
2. 固有名詞辞書・添付資料の情報をもとに、誤認識と思われる箇所を修正する
3. 話者情報は参考にして判断に活かすが、発言順序は絶対に変えない
4. 内容の要約・省略・補完・追記は一切しない
5. 句読点・敬語・体言止めのブレを統一する
6. 判断できない箇所は原文のままにする
7. 各セグメントを必ず同じJSON形式で返す"""

POLISH_USER_TEMPLATE = """{context}

【文字起こし（JSON形式）】
{segments_json}

上記の文字起こしを整えてください。speakerフィールドは参考情報です。
必ず以下のJSON形式で返してください（セグメント数・id は変えないこと）:
{{"segments": [{{"id": <id>, "text": "<整文後のテキスト>"}}]}}"""


@dataclass
class PolishResult:
    suggestions: dict[int, str]  # segment_id -> 整文後テキスト
    input_tokens: int
    output_tokens: int
    cost_yen: float


async def run_polish(
    client,
    segments: list[dict],
    context_text: str,
    model_key: str = "haiku",
) -> PolishResult:
    """整文を実行し、セグメントごとの提案テキストを返す。

    Args:
        client: anthropic.AsyncAnthropic インスタンス
        segments: [{"id": int, "text": str}, ...]
        context_text: build_context_text() で構築したコンテキスト
        model_key: "haiku" | "sonnet"
    """
    model_id = MODELS.get(model_key, MODELS["haiku"])["id"]

    segments_json = json.dumps(
        [{"id": s["id"], "speaker": s.get("speaker", ""), "text": s["text"]} for s in segments],
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
    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens

    # JSONパース
    try:
        data = json.loads(raw)
        suggestions = {s["id"]: s["text"] for s in data.get("segments", [])}
    except Exception:
        # JSONパース失敗時は提案なし（整文失敗として扱う）
        logger.error("整文レスポンスのJSONパース失敗: %s", raw[:500])
        suggestions = {}

    cost = actual_cost_yen(input_tokens, output_tokens, model_key)
    return PolishResult(
        suggestions=suggestions,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_yen=cost,
    )
