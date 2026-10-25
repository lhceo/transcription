"""整文（文字起こし校正）機能。

Claude API を使って粗い音声認識テキストを正確に整える。
要約・省略・補完は一切行わず、フィラー除去・誤認識修正・表記統一のみ実施する。
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r'https?://\S+')
_KANJI_RE = re.compile(r'[一-龯]')
_PARENTHETICAL_RE = re.compile(r'[（(].+?[）)]')
# 純粋な音の繰り返し（なるほど・そう・はい等の意味ある相槌は含めない）
_REPEAT_FILLER_RE = re.compile(
    r'^(うん|ん|あ|え|へぇ|ほぉ|まあ|いや){2,}[、。！？]?$'
)


def _is_filler_segment(text: str) -> bool:
    """整文バッチから除外すべきフィラーセグメントかどうかを判定する。

    （）で囲まれた内容はユーザーの意図的な補足（感情表現・固有名詞・状況説明等）
    なので必ず保持する。漢字を含む場合も意味があると判断して保持する。
    """
    t = text.strip()
    if not t:
        return True
    # （）内容を含む → ユーザーの意図的補足 → 保持
    if _PARENTHETICAL_RE.search(t):
        return False
    # 句読点・括弧を除いた実質テキスト
    core = re.sub(r'[、。！？…・「」（）【】\s]', '', t)
    if not core:
        return True
    # 漢字含む → 意味ある語句として保持（笑・確かに・結局・例えば 等）
    if _KANJI_RE.search(core):
        return False
    # 2文字以内 + 漢字なし → フィラー（で、まあ、うん、ー 等）
    if len(core) <= 2:
        return True
    # 純粋な音の繰り返し（うんうん、ああ 等）
    if _REPEAT_FILLER_RE.match(t):
        return True
    return False


def _is_trivial_suggestion(original: str, suggested: str) -> bool:
    """整文案が原文と実質同一かどうかを判定する。

    以下の差異は「変更なし」と見なしてユーザーに見せない:
    - 完全一致
    - NFKC正規化・スペース差のみ（全角半角統一等）
    - 語尾の句読点変化のみ（「うん、」→「うん。」等）
    """
    if original == suggested:
        return True
    _TRAILING_PUNCT = '、。！？…'
    def _n(s: str) -> str:
        s = s.rstrip(_TRAILING_PUNCT)
        return ' '.join(unicodedata.normalize('NFKC', s).split())
    return _n(original) == _n(suggested)


async def _fetch_url_text(url: str, max_chars: int = 2500) -> str | None:
    """URLをフェッチしてプレーンテキストを返す。失敗・非テキストはNone。"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Noto/1.0)"},
            )
            if resp.status_code != 200:
                return None
            ct = resp.headers.get("content-type", "")
            if "html" in ct:
                text = re.sub(r'<style[^>]*>.*?</style>', '', resp.text, flags=re.DOTALL)
                text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
            elif "text" in ct or "plain" in ct or "json" in ct:
                text = resp.text.strip()
            else:
                return None
            return text[:max_chars] if text else None
    except Exception as _e:
        logger.debug("URL fetch失敗 %s: %s", url, _e)
        return None


async def fetch_url_contexts(comments: list[str], max_urls: int = 5) -> list[dict]:
    """コメントリストからURLを抽出してフェッチし、{url, content} のリストを返す。"""
    seen: set[str] = set()
    urls: list[str] = []
    for comment in comments:
        for url in _URL_RE.findall(comment or ""):
            if url not in seen:
                seen.add(url)
                urls.append(url)
            if len(urls) >= max_urls:
                break
        if len(urls) >= max_urls:
            break

    results = []
    for url in urls:
        content = await _fetch_url_text(url)
        if content:
            results.append({"url": url, "content": content})
    return results

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

PICKUP_SYSTEM = """あなたは日本語ビジネス会議の文字起こしを分析するアシスタントです。
テキストから固有名詞・専門用語を抽出し、それぞれの意味を推察してください。

条件:
- 人名・会社名・製品名・プロジェクト名・略称・ニックネームなどを対象にする
- 既に辞書に登録済みの語句は除外する
- 一般的な日本語の単語（会議・報告・確認など）は除外する
- 最大15件まで

各単語について「それが何か」を会話の文脈から1〜2文で推察してください。
分からない場合は meaning を空文字にしてください。

必ずJSONで返してください（他のテキストは不要）:
{"words": [{"word": "単語1", "meaning": "推察される意味・説明"}, ...]}"""


async def pickup_proper_nouns(
    client,
    transcript_text: str,
    registered_words: list[str],
) -> tuple[list[dict], int, int]:
    """文字起こしテキストから未登録の固有名詞候補を抽出する。

    Returns:
        (words, input_tokens, output_tokens)
        words は [{"word": str, "meaning": str}, ...] 形式
    """
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
            max_tokens=1024,
            system=PICKUP_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        inp = message.usage.input_tokens if message.usage else 0
        out = message.usage.output_tokens if message.usage else 0
        try:
            data = json.loads(text)
        except json.JSONDecodeError as je:
            logger.warning("固有名詞ピックアップ: JSONパース失敗 (raw=%r) %s", text[:200], je)
            return [], inp, out
        raw_words = data.get("words", [])
        # 旧フォーマット（文字列リスト）と新フォーマット（dictリスト）の両対応
        words = [
            w if isinstance(w, dict) else {"word": w, "meaning": ""}
            for w in raw_words
            if (isinstance(w, dict) and w.get("word")) or (isinstance(w, str) and w)
        ]
        return words, inp, out
    except Exception as e:
        logger.warning("固有名詞ピックアップ失敗: %s", e)
        return [], 0, 0


def extract_word_contexts(words: list, transcript_text: str) -> list[dict]:
    """各単語の初出文脈（前後の文）を抽出する。

    Args:
        words: str のリスト（旧形式）または {"word": str, "meaning": str} のdictリスト（新形式）

    Returns:
        [{"word": str, "meaning": str, "context": str | None}, ...]
    """
    # セグメント区切りは改行、文区切りは。！？
    sentences = re.split(r'(?<=[。！？])|(?<=\n)', transcript_text)
    sentences = [s.strip() for s in sentences if s.strip()]

    results = []
    for item in words:
        word = item if isinstance(item, str) else item.get("word", "")
        meaning = "" if isinstance(item, str) else (item.get("meaning") or "")
        if not word:
            continue
        context: str | None = None
        for sentence in sentences:
            if word in sentence:
                idx = sentence.index(word)
                start = max(0, idx - 25)
                end = min(len(sentence), idx + len(word) + 25)
                snippet = sentence[start:end].strip()
                if start > 0:
                    snippet = "…" + snippet
                if end < len(sentence):
                    snippet = snippet + "…"
                context = snippet
                break
        results.append({"word": word, "meaning": meaning, "context": context})
    return results


# フィラー・繰り返し自動クリーニング ─────────────────────────────────────────

# 長音（ー）が付く場合のみフィラーと判定する。
# 長音なし（うん、えー→えなど）は意味のある発言になりうるので除外。
# えっと は例外的に長音なしでも常にフィラー。
_FILLER_PATTERNS = [
    r'うー+ん',     # うーん、うーーん
    r'んー+ん',     # んーん
    r'えー+と',     # えーと、えーーと
    r'えっと',      # えっと
    r'えー+',       # えー（単独）
    r'あのー+',     # あのー（長音付き = フィラー。長音なし「あの」は指示語なので除外）
    r'んー+と',     # んーと
    r'んー+',       # んー
    r'あー+',       # あー
    r'はー+',       # はー
    r'ふー+ん',     # ふーん
]

# セグメント区切り文字（フィラー前後に現れる）
_SEP = r'[、。！？\s]'

# コンパイル済みパターン（前後に区切り文字 or 文頭/文末）
_FILLER_RE = re.compile(
    r'(?:(?<=' + _SEP + r')|(?:^))(?:' + '|'.join(_FILLER_PATTERNS) + r')(?=' + _SEP + r'|$)',
)
_FILLER_STANDALONE_RE = re.compile(
    r'^(?:' + '|'.join(_FILLER_PATTERNS) + r')$'
)


def clean_fillers_and_repetitions(text: str) -> tuple[str, int]:
    """フィラーと直前繰り返しをルールベースで除去する。

    Returns:
        (cleaned_text, changes_count)
        changes_count: 変更があった箇所数（0なら変更なし）
    """
    if not text:
        return text, 0

    original = text
    change_count = 0

    # 完全にフィラーだけのセグメントはそのまま返す（空にしない）
    if _FILLER_STANDALONE_RE.match(text.strip()):
        return text, 0

    # Step 1: フィラー除去
    new_text, n = _FILLER_RE.subn('', text)
    if n > 0:
        change_count += n
        text = new_text

    # Step 2: 区切り文字の連続・先頭末尾の整理
    if change_count > 0:
        text = re.sub(r'[、。]{2,}', '、', text)
        text = re.sub(r'^[、。\s]+', '', text)
        text = re.sub(r'[、。\s]+$', '', text)

    # Step 3: 直前繰り返し除去（2文字以上の同一フレーズが、。または空白を挟んで連続）
    # 例: 「そうですね、そうですね」→「そうですね」
    prev = None
    while prev != text:
        prev = text
        new_text, n = re.subn(r'(.{2,})[、。 ]\1(?=[、。\s]|$)', r'\1', text)
        if n > 0:
            change_count += n
            text = new_text

    return text.strip() if change_count > 0 else original, change_count


# 品質チェック ────────────────────────────────────────────────────────────────

QUALITY_CHECK_SYSTEM = """あなたは日本語の音声認識・文字起こしの品質チェッカーです。
セグメントのリストを分析し、ユーザーが確認・修正すべき箇所を特定してください。

チェック対象（優先度の高い順）:
1. 固有名詞・社名・人名の音声認識誤り疑い（「クォーレ」→「クオーレ」のような読みの揺れ、同音異字の誤変換）
2. 断片的すぎて意味が不明瞭なセグメント（孤立した短い発言で文脈がない等）
3. 明らかに不自然な語句・前後の文脈と合わない変換

報告しないもの:
- フィラー（えー・あのー等）のみの発言
- 文法的に問題ない短い相槌（「はい」「そうですね」「分かりました」等）
- 内容的に問題ない普通の発言

返却形式（JSONのみ、前後に説明文を付けない）:
{"issues": [{"segment_id": N, "type": "proper_noun|fragment|unclear", "note": "確認してほしい理由（20字以内）"}]}
issuesが0件の場合: {"issues": []}
最大15件まで。"""


async def run_quality_check(
    client,
    segments: list[dict],
) -> list[dict]:
    """文字起こしセグメントの品質チェックを実行し、問題箇所リストを返す。

    Returns: [{"segment_id": int, "type": str, "note": str}]
    """
    lines = []
    for s in segments:
        lines.append(f'{{"segment_id": {s["id"]}, "speaker": {json.dumps(s.get("speaker", ""), ensure_ascii=False)}, "text": {json.dumps(s["text"], ensure_ascii=False)}}}')
    segments_text = "[\n" + ",\n".join(lines) + "\n]"

    prompt = f"以下の文字起こしセグメントをチェックしてください。\n\n{segments_text}"
    try:
        message = await client.messages.create(
            model=MODELS["haiku"]["id"],
            max_tokens=1024,
            system=QUALITY_CHECK_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        json_str = m.group(0) if m else text
        data = json.loads(json_str)
        return data.get("issues", [])
    except Exception as e:
        logger.warning("品質チェック失敗: %s", e)
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

【ルールA 厳守事項】
（）は原文に存在しない語句を新たに追加する場合のみ使う。
原文にすでに存在する語句を言い換えたり丁寧にしたりする場合は（）で囲まない。
例（NG）: 発言「嫁からしたら都会だとか」→ 整文「（妻からしたら）都会だとか」
  ← 「嫁からしたら」は原文にあるので（）不要。言い換えるだけなら直接置き換える。
例（OK）: 発言「嫁からしたら都会だとか」→ 整文「妻からしたら都会だとか」（言い換えのみ）
例（OK）: 発言「都会だとか」→ 整文「（妻からしたら）都会だとか」（主語が省略されている場合）

【ルールB】指示語（これ・それ・あれ・ここ・そこ・あの件・先日の・例のやつ 等）
→ 文脈から特定できる場合、具体的な語句に置き換え type:"pronoun_resolved" フラグを付与
  発言: 「それを進めておいて」
  整文: 「ランディングページの原稿を進めておいて」
→ 文脈から特定できない場合、元の表現を残し （？） を挿入し
  type:"unresolved_pronoun", words:["それ"] フラグを付与する

【ルールC】推察できない・確信が持てない場合
→ 元の表現を残し （？） を挿入する
  例: 「（？）対応してもらえますか」
  例: 「あの件（？）については次回確認します」
  ※ （？） は「参加者以外には判断できない箇所」のマーカー
→ 意味不明瞭な語句（固有名詞・略称・社内用語）は type:"meaning_unclear",
  words:["語句1","語句2"] フラグで列挙する（noteに説明を記載）

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
- 指示語を具体的な語句に置き換えた → type:"pronoun_resolved"
- 指示語の指示対象が不明で置き換えられなかった → type:"unresolved_pronoun", words:["それ","あの件"]
- 省略された主語・目的語を（補完）形式で追加した → type:"subject_added"
- 意味不明・文脈不明瞭な語句がある → type:"meaning_unclear", words:["語句1","語句2"]
- 固有名詞辞書の読みをもとに表記を変換した → type:"proper_noun_fixed", words:["変換語"]
wordsフィールド: UIでユーザーに該当語句をクリック登録させるために使う。必ず含めること。
noteには日本語で何を行ったか簡潔に説明すること。フラグがない場合はflags: []とすること。

【整文例】
入力: {"id": 1, "speaker": "市川 / ライオンハート / PM", "text": "えーと、それ、来週までにやっといて"}
出力: {"id": 1, "text": "（見積書を）来週までにまとめておいてください。", "flags": [{"type": "pronoun_resolved", "note": "「それ」を文脈から「見積書」と推察し補完しました"}, {"type": "subject_added", "note": "「見積書を」を補完しました"}]}

入力: {"id": 2, "speaker": "古瀬社長 / ライフバンク / 代表", "text": "あの件どうなってる？"}
出力: {"id": 2, "text": "（村プロジェクトの補助金申請の件は）どうなっていますか？", "flags": [{"type": "pronoun_resolved", "note": "「あの件」を「村プロジェクトの補助金申請の件」に置き換えました"}]}

入力: {"id": 3, "speaker": "蒲社長", "text": "えのさんが来週対応してくれるって言ってたよ"}
出力: {"id": 3, "text": "榎本計介（えのさん）が来週対応してくれると言っていました。", "flags": [{"type": "proper_noun_fixed", "words": ["えのさん"], "note": "「えのさん」を固有名詞辞書をもとに「榎本計介（えのさん）」に展開しました"}]}

入力: {"id": 4, "speaker": "営業Aさん", "text": "それはないですよね（笑）"}
出力: {"id": 4, "text": "それはないですよね（笑）", "flags": [{"type": "unresolved_pronoun", "words": ["それ"], "note": "「それ」の指示対象が文脈から特定できません。ユーザーによる補足が必要です"}]}

入力: {"id": 5, "speaker": "未設定話者3", "text": "アクセスさんところに行った時に"}
出力: {"id": 5, "text": "アクセスさんのところに行った時に", "flags": [{"type": "meaning_unclear", "words": ["アクセスさん"], "note": "「アクセスさん」が何を指すか判断できません（企業名・人名等）"}]}"""

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
{{"segments": [{{"id": <id>, "text": "<整文後のテキスト>", "flags": [{{"type": "<種別>", "words": ["<語句1>"], "note": "<説明>"}}]}}]}}"""


# 出力トークン閾値: これを超えると分割処理に切り替える
# max_tokens=8192 に対して安全マージンを確保。
# 実測でテキストトークンの他に JSON 構造オーバーヘッドが大きいため低めに設定。
_SAFE_OUTPUT_TOKENS = 4000
# 1セグメントあたりの JSON 構造オーバーヘッド (tokens):
# {"id":X,"text":"...","flags":[{"type":"Y","note":"Z"}]} のキー等 ≈ 40 tokens/seg
_PER_SEG_JSON_OVERHEAD = 40
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
        logger.info("整文バッチ完了: segs_in=%d suggestions=%d raw_len=%d", len(segments), len(suggestions), len(raw))
        if len(suggestions) == 0 and len(segments) > 0:
            logger.warning("整文バッチ: 提案が空 (0件). raw先頭=%s", raw[:300])
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

    # フィラーセグメントを除外（AIに送らない → コスト削減 + レビューノイズ低減）
    filler_ids = {s["id"] for s in segments if _is_filler_segment(s["text"])}
    if filler_ids:
        logger.info("フィラーセグメント除外: %d件", len(filler_ids))
    segments = [s for s in segments if s["id"] not in filler_ids]

    # ── 全文サマリー先行抽出（バッチをまたぐ文脈補完の精度向上）──────────────
    # 一括送信でも分割でも全バッチに同じサマリーを添付する
    meeting_summary = await extract_meeting_summary(client, segments)

    # 出力トークンを概算（テキストトークン × 1.2 + JSON構造オーバーヘッド）
    # _PER_SEG_JSON_OVERHEAD で {"id":X,"text":"...","flags":[...]} の固定部分を加算
    estimated_output = int(
        sum(estimate_tokens(s["text"]) * 1.2 + _PER_SEG_JSON_OVERHEAD for s in segments)
    )
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
            adopted = 0
            for seg_id, text in suggestions.items():
                if seg_id in own_ids:
                    merged[seg_id] = text
                    adopted += 1
            if adopted == 0:
                logger.warning("整文バッチ%d: own_ids=%d件に対して採用提案0件 (suggestions=%d件)", i, len(own_ids), len(suggestions))
            for seg_id, flag_list in batch_flags.items():
                if seg_id in own_ids:
                    merged_flags[seg_id] = flag_list

            # 次バッチのオーバーラップに使うため今バッチの整文済み結果を保持
            prev_suggestions = suggestions

            total_input += inp
            total_output += out
            if progress_callback:
                progress_callback(i + 1, n_batches)

    # 原文と実質同一の提案を除去：句読点の全半角差のみ等はユーザーに見せない
    seg_text_map = {s["id"]: s["text"] for s in segments}
    before_trivial_filter = len(merged)
    merged = {
        seg_id: text
        for seg_id, text in merged.items()
        if not _is_trivial_suggestion(seg_text_map.get(seg_id, ""), text)
    }
    trivial_removed = before_trivial_filter - len(merged)
    if trivial_removed > 0:
        logger.info("trivialフィルター: %d件を除去 (残%d件)", trivial_removed, len(merged))
    if len(merged) == 0 and before_trivial_filter > 0:
        logger.warning("trivialフィルター後に提案が0件になりました (除去前=%d件) — 原文がすでにきれいな可能性あり", before_trivial_filter)
    merged_flags = {
        seg_id: flags
        for seg_id, flags in merged_flags.items()
        if seg_id in merged
    }

    cost = actual_cost_yen(total_input, total_output, model_key)
    return PolishResult(
        suggestions=merged,
        flags=merged_flags,
        input_tokens=total_input,
        output_tokens=total_output,
        cost_yen=cost,
    )
