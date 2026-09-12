"""整文（文字起こし校正）機能。

Claude API を使って粗い音声認識テキストを正確に整える。
要約・省略・補完は一切行わず、フィラー除去・誤認識修正・表記統一のみ実施する。
"""

from __future__ import annotations

import json
import logging
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
- 発言の言葉・内容・順序は絶対に変えない
- 要約・省略・言い換え・内容の追記は一切しない
- [＝○○] 形式の注釈のみを使って文脈を補う

【整文の手順】

ステップ1: フィラー除去
えー・あのー・うーん・まあ・えっと・なんか（間投詞として使われている場合）等を除去する。
ただし「まあ、そうですね」のように意味のある使い方はそのまま残す。

ステップ2: 音声認識の誤認識修正
固有名詞辞書・前後の文脈をもとに、明らかな誤認識を修正する。
辞書に登録済みの語句（例: 「のーと」→「Noto」）を優先的に確認する。

ステップ3: 句読点・表記の統一
句読点の欠落を補い、敬体（です・ます）か常体かのブレを統一する。

ステップ4: 指示語・暗黙コンテキストの注釈（最重要）
MTGでは「これ」「それ」「あの件」など、非言語コミュニケーション（指差し・画面共有・
共有知識）で成立する表現が多く、文字だけでは意味が通じない。
以下の手順で注釈を付ける:

対象語句: これ・それ・あれ・ここ・そこ・あちら・こちら・あの件・先日の・例のやつ・
         先ほどの・〜の部分・〜のところ 等

推察の根拠（優先順）:
  1. 直前の発言・話題の流れ
  2. 話者の役職・会社・プロジェクト役割（speakerフィールド参照）
  3. MTGのアジェンダ・目的
  4. 固有名詞辞書

注釈フォーマット:
  - 推察できる場合: 「それ[＝ランディングページの原稿修正]を進めてください」
  - 推察できない場合: 「それ[＝？]を進めてください」
  ※ 指示語を無注釈で残さない。必ずどちらかの形で注釈を付ける。

ステップ5: 話者属性の活用
speakerに含まれる会社・役職・役割の情報を推察の補強に使う。
例: クライアント企業の意思決定者の「それで問題ありません」は承認として解釈できる。
例: 受注者側のPMの「確認します」は社内確認のアクションとして解釈できる。

ステップ6: 判断できない箇所は原文のまま残す（推測で内容を書き換えない）

ステップ7: 各セグメントを必ず同じJSON形式で返す"""

POLISH_USER_TEMPLATE = """{context}

【文字起こし（JSON形式）】
{segments_json}

上記の文字起こしを整文してください。
speakerフィールドの話者名・会社・役職・役割をコンテキスト推察に積極的に活用してください。
指示語（これ・それ・あれ等）には必ず [＝推察内容] または [＝？] を付けてください。

必ず以下のJSON形式のみで返してください（前後に説明文を付けないこと。セグメント数・idは変えないこと）:
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

    # JSONパース（Claudeが前後に説明文を付けることがあるため正規表現で抽出）
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        json_str = m.group(0) if m else raw
        data = json.loads(json_str)
        suggestions = {s["id"]: s["text"] for s in data.get("segments", [])}
    except Exception:
        logger.error("整文レスポンスのJSONパース失敗: %s", raw[:500])
        suggestions = {}

    cost = actual_cost_yen(input_tokens, output_tokens, model_key)
    return PolishResult(
        suggestions=suggestions,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_yen=cost,
    )
