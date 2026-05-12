"""テキスト整形ユーティリティ。

AssemblyAI は日本語テキストに対して 1 文字ごとや単語境界ごとに半角スペースを
挿入することがある（例: "その 人 も やっぱり 京都"）。日本語として自然な表現
ではないため、保存前に正規化する。
"""

from __future__ import annotations

import re

# CJK 文字（ひらがな・カタカナ・漢字・全角記号）。
# AssemblyAI が間に入れた不要な半角空白を見分けるための文字クラス。
_CJK_CHAR = (
    r"["
    r"぀-ゟ"  # ひらがな
    r"゠-ヿ"  # カタカナ
    r"一-鿿"  # CJK 統合漢字
    r"　-〿"  # CJK 記号と句読点
    r"＀-￯"  # 半角・全角形
    r"]"
)

# 「CJK 文字 + 半角空白 + CJK 文字」のパターン
_CJK_SPACE_PATTERN = re.compile(rf"({_CJK_CHAR})[ \t]+({_CJK_CHAR})")


def normalize_japanese_text(text: str) -> str:
    """日本語文字に挟まれた不要な半角空白を取り除く。

    例: "その 人 も やっぱり 京都" → "その人もやっぱり京都"

    英単語間のスペース、全角文字と半角文字の間のスペースなどは保持する。
    例: "AI と GPT" → "AI と GPT" のまま（CJK のひらがな「と」の両側は
    半角英字なので影響を受けない）

    実装メモ:
    - 連続したマッチを取り除くため、変化がなくなるまでループする
    - re.sub のオーバーラップしないマッチでは "あ い う" → "あい う" となる
      ので、再度 sub することで "あいう" にする
    """
    if not text:
        return text
    prev = ""
    current = text
    # 安全のため最大ループ数を制限（通常 3〜4 回で収束する）
    for _ in range(50):
        prev = current
        current = _CJK_SPACE_PATTERN.sub(r"\1\2", current)
        if current == prev:
            break
    # 行頭/行末の空白を取り除く（複数行のテキストも処理）
    return "\n".join(line.strip() for line in current.split("\n"))
