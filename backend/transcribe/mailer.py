"""Gmail SMTP を使った通知メール送信モジュール。

必要な環境変数:
  SMTP_USER     — 送信元 Gmail アドレス (例: noto@lionheart.co.jp)
  SMTP_PASSWORD — Gmail App パスワード (16文字)

未設定の場合は送信をスキップし、ログに記録する。
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def _send_sync(
    smtp_user: str,
    smtp_password: str,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Noto <{smtp_user}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as server:
        server.ehlo()
        server.starttls(context=ctx)
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_email, msg.as_string())


async def send_share_notification(
    *,
    smtp_user: str,
    smtp_password: str,
    to_email: str,
    to_name: str,
    sharer_name: str,
    transcript_title: str,
    transcript_url: str,
) -> None:
    """共有通知メールを非同期で送信する。失敗してもログに記録するだけで例外は伝搬しない。"""
    if not smtp_user or not smtp_password:
        logger.info("SMTP 未設定のため共有通知メールをスキップ: %s", to_email)
        return

    subject = f"[Noto] {sharer_name} さんが文字起こしを共有しました"

    body_text = (
        f"{sharer_name} さんがあなたと文字起こしを共有しました。\n\n"
        f"「{transcript_title}」\n\n"
        f"以下のリンクから確認できます:\n{transcript_url}\n\n"
        "---\nNoto"
    )

    body_html = f"""<!DOCTYPE html>
<html lang="ja">
<body style="font-family:sans-serif;color:#1e293b;max-width:480px;margin:0 auto;padding:24px">
  <p style="margin-bottom:16px">
    <strong>{sharer_name}</strong> さんがあなたと文字起こしを共有しました。
  </p>
  <div style="background:#f8fafc;border-radius:8px;padding:16px 20px;margin-bottom:20px">
    <p style="margin:0;font-weight:600">「{transcript_title}」</p>
  </div>
  <a href="{transcript_url}"
     style="display:inline-block;background:#3b82f6;color:#fff;text-decoration:none;
            padding:10px 20px;border-radius:8px;font-weight:600">
    文字起こしを確認する →
  </a>
  <p style="margin-top:32px;font-size:12px;color:#94a3b8">Noto</p>
</body>
</html>"""

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _send_sync,
            smtp_user, smtp_password, to_email, subject, body_text, body_html,
        )
        logger.info("共有通知メール送信完了: %s → %s", smtp_user, to_email)
    except Exception as exc:
        logger.error("共有通知メール送信失敗 (%s): %s", to_email, exc)
