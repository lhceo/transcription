"""Chatwork 通知ヘルパー。

Anthropic クレジット残高が閾値を下回ったときに管理者へ通知する。
必要な環境変数:
  CHATWORK_API_TOKEN — Chatwork API トークン
  CHATWORK_ROOM_ID   — 通知先のルームID
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from backend.transcribe.cost import AnthropicBalanceSummary

logger = logging.getLogger(__name__)

# 最後にアラートを送った時刻（サーバー再起動でリセットされる）
_last_alert_ts: float = 0


async def send_message(room_id: str, api_token: str, body: str) -> None:
    """Chatwork のルームにメッセージを送信する。失敗はログのみ。"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.chatwork.com/v2/rooms/{room_id}/messages",
                headers={"X-ChatWorkToken": api_token},
                data={"body": body},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Chatwork 送信成功: room=%s", room_id)
    except Exception as e:
        logger.error("Chatwork 送信失敗: %s", e)


async def maybe_notify_low_anthropic_credit(remaining_yen: int) -> None:
    """残高が閾値を下回っていたら Chatwork に通知する（24時間に1回まで）。"""
    global _last_alert_ts

    from backend.config import load_settings
    from backend.transcribe.cost import get_anthropic_balance_summary
    settings = load_settings()

    if not settings.has_chatwork or not settings.has_anthropic_credit:
        return

    balance = get_anthropic_balance_summary()
    if not balance.is_low:
        return

    now = time.time()
    if now - _last_alert_ts < 86400:
        logger.debug("Chatwork アラートスキップ（24h以内に送信済み）")
        return

    alert_pretty = _fmt_alert(balance)
    body = (
        "[info][title]⚠️ Noto: Anthropic API クレジット残高が少なくなっています[/title]"
        f"現在の残高: {balance.remaining_pretty}\n"
        f"（残高が {alert_pretty} を下回りました）\n\n"
        "整文・品質チェック機能を継続するには、クレジットを追加してください。\n"
        "https://console.anthropic.com\n\n"
        "追加後は Railway の環境変数 ANTHROPIC_CREDIT_USD（または YEN）と ANTHROPIC_CREDIT_SINCE を更新してください。"
        "[/info]"
    )
    await send_message(settings.chatwork_room_id, settings.chatwork_api_token, body)
    _last_alert_ts = now
    logger.info("Chatwork 低残高通知送信: remaining=%s alert=%s", balance.remaining_pretty, alert_pretty)


def _fmt_alert(balance: "AnthropicBalanceSummary") -> str:
    if balance.currency == "USD":
        return f"${balance.alert_yen / balance.usd_jpy_rate:.2f}"
    return f"¥{balance.alert_yen:,}"
