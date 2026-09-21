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
            reading = v.get("reading") or ""
            meaning = v.get("meaning") or ""
            entry = word
            if reading:
                entry += f"（読み: {reading}）"
            if meaning:
                entry += f": {meaning}"
            lines.append(f"  - {entry}")
        lines.append("【固有名詞辞書の使い方】")
        lines.append("・単語欄が正式表記。読み欄がある場合は音声認識の揺らぎ検出に最優先で使用すること")
        lines.append("・音声認識の揺らぎ（発音が似た誤認識・カタカナ転写・長音伸ばし等）をこの表記に統一すること")
        lines.append("・意味欄は登場人物の役割・関係性の文脈理解に使うこと。「誰が・誰に」の補完精度を高める")
        lines.append("・意味に正式名が含まれるニックネームは、初出時に「正式名（ニックネーム）」形式で展開すること")

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


# 全文サマリー先行抽出 ────────────────────────────────────────────────────────

SUMMARY_SYSTEM = """あなたは日本語のMTG文字起こしを分析するアシスタントです。
整文の前処理として、会議の全体像を把握するためのサマリーを作成します。

【厳守事項】
- 発言内容を推測・創作しない
- テキストに明示されていることのみ箇条書きで列挙する
- 200字以内に収める"""

SUMMARY_USER_TEMPLATE = """以下のMTG文字起こし全文を読んで、整文アシスタントが「誰が・誰に・何を」を正確に補完できるよう、以下の4点を箇条書きで簡潔にまとめてください。

1. 会議で扱われた主なトピック（最大5件）
2. 登場する人物・組織と役割（テキストから読み取れるもの）
3. 繰り返し言及されたキーワード・固有名詞
4. 決定事項・アクション（明示されているもののみ）

【文字起こし全文】
{transcript_text}

箇条書きのみで返してください。説明文・前置き不要。"""


async def extract_meeting_summary(
    client,
    segments: list[dict],
) -> str:
    """整文前に全セグメントを軽量モデルで要約し、会議の全体像を返す。

    各バッチのコンテキストに添付することで、バッチをまたぐ指示語・省略の
    推察精度を向上させる。失敗しても整文は継続（空文字を返す）。
    """
    # 全セグメントを「話者: テキスト」形式で結合
    lines = []
    for s in segments:
        speaker = s.get("speaker", "")
        text = s.get("text", "")
        lines.append(f"{speaker}: {text}" if speaker else text)
    full_text = "\n".join(lines)

    # 長すぎる場合は先頭・中間・末尾からサンプリング（Haikuの入力上限を考慮）
    max_chars = 12000
    if len(full_text) > max_chars:
        chunk = max_chars // 3
        mid = len(full_text) // 2
        full_text = (
            full_text[:chunk] + "\n…（中略）…\n" +
            full_text[mid - chunk // 2: mid + chunk // 2] + "\n…（中略）…\n" +
            full_text[-chunk:]
        )

    prompt = SUMMARY_USER_TEMPLATE.format(transcript_text=full_text)
    logger.info("全文サマリー抽出開始: segs=%d chars=%d", len(segments), len(full_text))
    try:
        message = await client.messages.create(
            model=MODELS["haiku"]["id"],
            max_tokens=512,
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        summary = message.content[0].text.strip()
        logger.info("全文サマリー抽出完了: %d字", len(summary))
        return summary
    except Exception as e:
        logger.warning("全文サマリー抽出失敗（整文は継続）: %s", e)
        return ""


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
固有名詞辞書の各エントリについて、読み欄（カタカナ読み）をもとに、発音が似た表記で
誤認識されている箇所がないかテキスト全体を照合し、正式表記に置換する。
例: 「のーと」→「Noto」、「らいおんはーと」→「ライオンハート」
読み欄がない場合も、カタカナ転写・長音伸ばし・音の類似などで誤認識されていないか確認する。

ステップ2.5: 同音異義語の文意チェック（音声認識に特有の誤り）
音声認識は「音」を正しく拾っても、文脈に合わない漢字を当ててしまう場合がある。
下記の「業務日本語 同音異義語マスターリスト」を参照し、前後の文脈・会議サマリー・
話者の役職を根拠に、意味が合わない表記を正しい漢字に修正する。
確信が持てない場合は元の表記を残す（推測で書き換えない）。

【業務日本語 同音異義語マスターリスト】

▍動詞（口語では特に誤変換が多い）
・とる      採用・収集の文脈 →「採る」／ 入手・把握の文脈 →「取る」
            例:「人を採っていく」「候補者を採る」「情報を取る」
・はかる    解決・改善・実現を目指す →「図る」／ 数値の測定 →「測る」「計る」／ 悪意 →「謀る」
            例:「効率を図る」「利益を図る」「時間を計る」
・つとめる  役職・役割を担う →「務める」／ 会社に属する →「勤める」／ 努力する →「努める」
            例:「議長を務める」「会社に勤める」「改善に努める」
・すすめる  物事を前進させる →「進める」／ 推薦する →「薦める」／ 行動を促す →「勧める」
            例:「プロジェクトを進める」「候補者を薦める」「参加を勧める」
・おさめる  成果を得る →「収める」／ 支払う →「納める」／ 統治する →「治める」／ 習得する →「修める」
            例:「成功を収める」「税を納める」
・かえる    性質・状態の変化 →「変える」／ 交換 →「替える」／ 代替 →「代える」／ 両替・転換 →「換える」
            例:「方針を変える」「担当を替える」「言葉に換える」
・いかす    能力・特性を発揮させる →「活かす」（ビジネス文書の標準表記）
            例:「経験を活かす」「強みを活かす」

▍名詞（五十音順）
・いがい    除外・範囲外 →「以外」／ 予想外・驚き →「意外」
・いし      強い決意・意欲 →「意志」／ 考え・気持ち・希望 →「意思」
            例:「強い意志を持つ」「意思決定」「意思を確認する」
・いじょう  これより上・終了 →「以上」／ 通常でない状態 →「異常」
・いらい    頼むこと →「依頼」／ ある時点からずっと →「以来」
            例:「依頼を受ける」「入社以来」
・かいせい  法律・規則を改める →「改正」／ 天気が回復する →「快晴」
・かだい    解決すべき問題 →「課題」／ 大きすぎる →「過大」
            例:「課題を解決する」「過大な期待」
・かんしん  興味・注目 →「関心」／ 感動・称賛 →「感心」
            例:「関心を持つ」「感心した」
・きかい    チャンス・タイミング →「機会」／ マシン・装置 →「機械」
・きかん    時間の範囲・期限 →「期間」／ 組織・団体 →「機関」／ 身体の器官 →「器官」
            例:「期間限定」「研究機関」「内臓器官」
・きょうりょく 一緒に助け合う →「協力」／ 力が非常に強い →「強力」
            例:「協力をお願いする」「強力なツール」
・こうか    結果・効き目 →「効果」／ 値段が高い →「高価」／ 人事査定 →「考課」
            例:「効果を検証する」「高価な機材」「人事考課」
・しえん    サポート・援助 →「支援」
・しさく    政策・方針・手立て →「施策」／ 試しに作る →「試作」
            例:「施策を打つ」「施策の効果」「試作品」
・せいか    成し遂げた結果 →「成果」
            例:「成果を出す」「成果物」
・せいさく  映像・コンテンツ・広告物を作る →「制作」／ 物品・製品を作る →「製作」／ 行政の方針 →「政策」
            例:「動画を制作する」「製品を製作する」「政策を立案する」
・たいしょう ターゲット・対象物 →「対象」／ 左右対称 →「対称」／ 比べること →「対照」
            例:「対象者」「対照的」
・たいせい  組織の仕組み・構造 →「体制」／ 準備・備えの状態 →「態勢」／ 大多数 →「大勢」
            例:「営業体制を整える」「受け入れ態勢」「大勢の意見」
・ついきゅう 責任・原因を問い詰める →「追及」／ 目標・利益を目指す →「追求」／ 真理・事実を探る →「追究」
            例:「責任を追及する」「利益を追求する」「真相を追究する」
・とうし    資金を投じる →「投資」／ 関係する立場 →「当事（者）」
            例:「投資対効果」「当事者意識」
・ひょうか  判断・アセスメント →「評価」（「氷河」は地理用語で業務では出ない）
            例:「高く評価する」「評価制度」
・れんけい  協力して取り組む →「連携」（「連系」は電力系統の専門用語）
            例:「他部署と連携する」「連携を強化する」

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
  2. 固有名詞辞書（人名・会社名・プロジェクト固有語の意味欄を参照）
  3. 話者の役職・会社・プロジェクト役割（speakerフィールド参照）
  4. MTGのアジェンダ・目的・概要

ステップ5: 話者属性の活用
speakerに含まれる会社・役職・役割の情報を推察の補強に使う。
例: クライアント企業の意思決定者の「それで問題ありません」は承認として解釈できる。
例: 受注者側のPMの「確認します」は社内確認のアクションとして解釈できる。

ステップ6: 判断できない箇所は原文のまま残す（推測で内容を書き換えない）

ステップ7: 各セグメントをフラグ付きのJSON形式で返す
以下の操作を行った場合、対応するフラグを付与すること:
- 指示語（これ・それ・あれ・あの件等）を具体的な語句に置き換えた → type: "pronoun_resolved"
- 省略された主語・目的語・間接目的語を（補完）形式で追加した → type: "subject_added"
- テキストが意味不明・断片的で前後の文脈から内容を推察して書いた → type: "meaning_unclear"
- 固有名詞辞書の読みをもとに表記を変換した → type: "proper_noun_fixed"
noteには日本語で何を行ったか簡潔に説明すること。フラグがない場合はflags: []とすること。

【整文例】
入力: {"id": 1, "speaker": "市川 / ライオンハート / PM", "text": "えーと、それ、来週までにやっといて"}
出力: {"id": 1, "text": "（見積書を）来週までにまとめておいてください。", "flags": [{"type": "pronoun_resolved", "note": "「それ」を文脈から「見積書」と推察し補完しました"}, {"type": "subject_added", "note": "「見積書を」を補完しました"}]}

入力: {"id": 2, "speaker": "古瀬社長 / ライフバンク / 代表", "text": "あの件どうなってる？"}
出力: {"id": 2, "text": "（村プロジェクトの補助金申請の件は）どうなっていますか？", "flags": [{"type": "pronoun_resolved", "note": "「あの件」を「村プロジェクトの補助金申請の件」に置き換えました"}]}

入力: {"id": 3, "speaker": "蒲社長", "text": "えのさんが来週対応してくれるって言ってたよ"}
出力: {"id": 3, "text": "榎本計介（えのさん）が来週対応してくれると言っていました。", "flags": [{"type": "proper_noun_fixed", "note": "「えのさん」を固有名詞辞書をもとに「榎本計介（えのさん）」に展開しました"}]}"""

POLISH_USER_TEMPLATE = """{context}
{meeting_summary}
【文字起こし（JSON形式）】
※ commentフィールドがある場合、それはユーザーが記録した補足メモです。整文の文脈理解に活用してください。
{segments_json}

上記の文字起こしを整文してください。
speakerフィールドの話者名・会社・役職・役割をコンテキスト推察に積極的に活用してください。
「誰が・誰に・何を」を最優先で明確化し、省略された主語・目的語は （補完内容） 形式で文中に挿入してください。
指示語（これ・それ・あれ等）は具体的な語句に置き換えてください。
推察できない箇所のみ （？） を挿入してください。

必ず以下のJSON形式のみで返してください（前後に説明文を付けないこと。セグメント数・idは変えないこと）:
{{"segments": [{{"id": <id>, "text": "<整文後のテキスト>", "flags": [{{"type": "<種別>", "note": "<説明>"}}]}}]}}"""


# 出力トークン閾値: これを超えると分割処理に切り替える
# max_tokens=8192 に対して JSON オーバーヘッド・注釈膨張を考慮した安全値
_SAFE_OUTPUT_TOKENS = 6000
# バッチ間のオーバーラップ（文脈保持のため前バッチ末尾を次バッチ先頭に重複させる）
_OVERLAP_SEGMENTS = 5


@dataclass
class PolishResult:
    suggestions: dict[int, str]   # segment_id -> 整文後テキスト
    flags: dict[int, list[dict]]  # segment_id -> [{type, note}]
    input_tokens: int
    output_tokens: int
    cost_yen: float


async def _run_polish_batch(
    client,
    segments: list[dict],
    context_text: str,
    model_id: str,
    meeting_summary: str = "",
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
    summary_block = (
        f"【会議全体のサマリー（整文の文脈理解に活用してください）】\n{meeting_summary}\n\n"
        if meeting_summary else ""
    )
    prompt = POLISH_USER_TEMPLATE.format(
        context=context_text if context_text else "（コンテキスト情報なし）",
        meeting_summary=summary_block,
        segments_json=segments_json,
    )
    logger.info("整文バッチ開始: model=%s segs=%d prompt_chars=%d", model_id, len(segments), len(prompt))
    try:
        # ストリーミングモードを使用: 長時間リクエストでもタイムアウトしない
        async with client.messages.stream(
            model=model_id,
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": POLISH_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        ) as stream:
            raw = await stream.get_final_text()
            final_msg = await stream.get_final_message()
    except Exception as e:
        logger.error("整文バッチAPI呼び出し失敗: type=%s err=%s", type(e).__name__, e, exc_info=True)
        raise

    raw = raw.strip()
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        json_str = m.group(0) if m else raw
        data = json.loads(json_str)
        suggestions = {s["id"]: s["text"] for s in data.get("segments", [])}
        flags = {s["id"]: s["flags"] for s in data.get("segments", []) if s.get("flags")}
    except Exception:
        logger.error("整文レスポンスのJSONパース失敗: %s", raw[:500])
        suggestions = {}
        flags = {}
    return suggestions, flags, final_msg.usage.input_tokens, final_msg.usage.output_tokens


async def run_polish(
    client,
    segments: list[dict],
    context_text: str,
    model_key: str = "haiku",
    progress_callback=None,
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

    # ── 全文サマリー先行抽出（バッチをまたぐ文脈補完の精度向上）──────────────
    # 一括送信でも分割でも全バッチに同じサマリーを添付する
    meeting_summary = await extract_meeting_summary(client, segments)

    # 出力トークンを概算（日本語 0.7 tok/char × 注釈膨張 1.2 倍）
    estimated_output = int(sum(estimate_tokens(s["text"]) for s in segments) * 1.2)
    logger.info("整文 出力トークン概算: %s segs → %s tok (threshold %s)", len(segments), estimated_output, _SAFE_OUTPUT_TOKENS)

    total_input = 0
    total_output = 0
    merged: dict[int, str] = {}
    merged_flags: dict[int, list[dict]] = {}

    if estimated_output <= _SAFE_OUTPUT_TOKENS:
        # 一括送信
        suggestions, flags, inp, out = await _run_polish_batch(
            client, segments, context_text, model_id, meeting_summary=meeting_summary
        )
        merged.update(suggestions)
        merged_flags.update(flags)
        total_input += inp
        total_output += out
        if progress_callback:
            progress_callback(1, 1)
    else:
        # 必要最小限のバッチ数に分割
        n_batches = math.ceil(estimated_output / _SAFE_OUTPUT_TOKENS)
        batch_size = math.ceil(len(segments) / n_batches)
        logger.info("整文 分割処理: %s バッチ (各 %s セグメント + オーバーラップ %s)", n_batches, batch_size, _OVERLAP_SEGMENTS)

        # 前バッチの整文済み結果を保持（オーバーラップセグメントの text を差し替えるため）
        prev_suggestions: dict[int, str] = {}

        for i in range(n_batches):
            own_start = i * batch_size
            own_end = min(len(segments), (i + 1) * batch_size)
            # オーバーラップ: 前バッチ末尾を先頭に重複させて文脈を保持
            fetch_start = max(0, own_start - _OVERLAP_SEGMENTS)

            # オーバーラップセグメントの text を整文済みテキストに置き換えて文脈精度を向上させる
            batch_segs = []
            for s in segments[fetch_start:own_end]:
                if s["id"] in prev_suggestions:
                    # オーバーラップ部分は整文済みテキストを渡す（idはそのまま）
                    batch_segs.append({**s, "text": prev_suggestions[s["id"]]})
                else:
                    batch_segs.append(s)

            suggestions, batch_flags, inp, out = await _run_polish_batch(
                client, batch_segs, context_text, model_id, meeting_summary=meeting_summary
            )

            # オーバーラップ部分（前バッチが担当）は採用しない
            own_ids = {s["id"] for s in segments[own_start:own_end]}
            for seg_id, text in suggestions.items():
                if seg_id in own_ids:
                    merged[seg_id] = text
            for seg_id, flag_list in batch_flags.items():
                if seg_id in own_ids:
                    merged_flags[seg_id] = flag_list

            # 次バッチのオーバーラップに使うため今バッチの整文済み結果を保持
            prev_suggestions = suggestions

            total_input += inp
            total_output += out
            if progress_callback:
                progress_callback(i + 1, n_batches)

    cost = actual_cost_yen(total_input, total_output, model_key)
    return PolishResult(
        suggestions=merged,
        flags=merged_flags,
        input_tokens=total_input,
        output_tokens=total_output,
        cost_yen=cost,
    )
