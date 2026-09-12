"""Backblaze B2 への自動バックアップ (Tier 2)。

B2 ネイティブ API を httpx で叩く実装。boto3 不要。
必要な環境変数:
  B2_KEY_ID       — Application Key ID
  B2_APP_KEY      — Application Key
  B2_BUCKET_NAME  — バケット名 (例: transcription-backup)

動作:
  - 毎日深夜 2:00 (UTC) に SQLite を hot backup してアップロード
  - ファイル名: transcription_YYYY-MM-DD.sqlite
  - 直近 7 世代を保持、古いものは削除
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_KEEP_GENERATIONS = 7
_B2_AUTH_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"


def _is_configured() -> bool:
    return bool(
        os.environ.get("B2_KEY_ID")
        and os.environ.get("B2_APP_KEY")
        and os.environ.get("B2_BUCKET_NAME")
    )


async def _authorize(client: httpx.AsyncClient) -> dict:
    key_id = os.environ["B2_KEY_ID"]
    app_key = os.environ["B2_APP_KEY"]
    creds = base64.b64encode(f"{key_id}:{app_key}".encode()).decode()
    resp = await client.get(
        _B2_AUTH_URL,
        headers={"Authorization": f"Basic {creds}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


async def _get_upload_url(client: httpx.AsyncClient, auth: dict, bucket_id: str) -> dict:
    resp = await client.post(
        f"{auth['apiInfo']['storageApi']['apiUrl']}/b2api/v3/b2_get_upload_url",
        json={"bucketId": bucket_id},
        headers={"Authorization": auth["authorizationToken"]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


async def _get_bucket_id(client: httpx.AsyncClient, auth: dict, bucket_name: str) -> str:
    resp = await client.post(
        f"{auth['apiInfo']['storageApi']['apiUrl']}/b2api/v3/b2_list_buckets",
        json={"accountId": auth["accountId"], "bucketName": bucket_name},
        headers={"Authorization": auth["authorizationToken"]},
        timeout=30,
    )
    resp.raise_for_status()
    buckets = resp.json().get("buckets", [])
    if not buckets:
        raise RuntimeError(f"B2バケット '{bucket_name}' が見つかりません")
    return buckets[0]["bucketId"]


async def _upload_file(
    client: httpx.AsyncClient,
    upload_info: dict,
    file_name: str,
    data: bytes,
) -> None:
    sha1 = hashlib.sha1(data).hexdigest()
    resp = await client.post(
        upload_info["uploadUrl"],
        content=data,
        headers={
            "Authorization": upload_info["authorizationToken"],
            "X-Bz-File-Name": file_name,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
            "X-Bz-Content-Sha1": sha1,
        },
        timeout=120,
    )
    resp.raise_for_status()


async def _delete_old_backups(
    client: httpx.AsyncClient, auth: dict, bucket_id: str
) -> None:
    api_url = auth["apiInfo"]["storageApi"]["apiUrl"]
    resp = await client.post(
        f"{api_url}/b2api/v3/b2_list_file_names",
        json={
            "bucketId": bucket_id,
            "prefix": "transcription_",
            "maxFileCount": 100,
        },
        headers={"Authorization": auth["authorizationToken"]},
        timeout=30,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    # 名前でソート（日付順）、古いものから削除
    files.sort(key=lambda f: f["fileName"])
    to_delete = files[:-_KEEP_GENERATIONS] if len(files) > _KEEP_GENERATIONS else []
    for f in to_delete:
        del_resp = await client.post(
            f"{api_url}/b2api/v3/b2_delete_file_version",
            json={"fileId": f["fileId"], "fileName": f["fileName"]},
            headers={"Authorization": auth["authorizationToken"]},
            timeout=30,
        )
        if del_resp.is_success:
            logger.info("B2 古いバックアップを削除: %s", f["fileName"])


async def run_backup(db_path: Path) -> None:
    if not _is_configured():
        logger.info("B2バックアップ: 環境変数未設定のためスキップ")
        return
    if not db_path.exists():
        logger.warning("B2バックアップ: DB が見つかりません: %s", db_path)
        return

    bucket_name = os.environ["B2_BUCKET_NAME"]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    file_name = f"transcription_{date_str}.sqlite"

    logger.info("B2バックアップ開始: %s → %s", db_path, file_name)
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        # sqlite3.backup でホットバックアップ（ロック不要）
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(tmp_path))
        src.backup(dst)
        dst.close()
        src.close()

        data = tmp_path.read_bytes()
        tmp_path.unlink(missing_ok=True)

        async with httpx.AsyncClient() as client:
            auth = await _authorize(client)
            bucket_id = await _get_bucket_id(client, auth, bucket_name)
            upload_info = await _get_upload_url(client, auth, bucket_id)
            await _upload_file(client, upload_info, file_name, data)
            await _delete_old_backups(client, auth, bucket_id)

        logger.info("B2バックアップ完了: %s (%d bytes)", file_name, len(data))

    except Exception as e:
        logger.error("B2バックアップ失敗: %s", e)
        if "tmp_path" in locals():
            tmp_path.unlink(missing_ok=True)


async def backup_loop(db_path: Path) -> None:
    """毎日 UTC 02:00 にバックアップを実行するループ。"""
    # 起動直後に1回実行（初回確認用）
    await asyncio.sleep(60)
    await run_backup(db_path)

    while True:
        now = datetime.now(timezone.utc)
        # 次の 02:00 UTC まで待つ
        next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run = next_run.replace(day=next_run.day + 1)
        wait_secs = (next_run - now).total_seconds()
        logger.info("B2バックアップ: 次回実行まで %.0f 秒", wait_secs)
        await asyncio.sleep(wait_secs)
        await run_backup(db_path)
