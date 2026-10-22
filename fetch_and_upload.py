#!/usr/bin/env python3
"""Collect Telegram MTProto proxy links and upload them to UploadKon."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

TEHRAN = ZoneInfo("Asia/Tehran")
DEFAULT_UPLOAD_URL = "https://my.uploadkon.ir/api/v1/file-entries"
DEFAULT_LOOKBACK_HOURS = 12
# Telegram proxy links are commonly shared as tg://proxy or t.me/proxy URLs.
LINK_RE = re.compile(r"(?:tg://proxy|https?://(?:t\.me|telegram\.me)/proxy)\?[^\s<>\"'`]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ").,;!?]}»'\""


def extract_proxy_configs(text: str) -> list[str]:
    """Return canonical tg://proxy links containing server, port and secret."""
    configs: list[str] = []
    seen: set[str] = set()
    for match in LINK_RE.finditer(text or ""):
        raw = match.group(0).rstrip(TRAILING_PUNCTUATION)
        try:
            query = parse_qs(urlsplit(raw).query, keep_blank_values=True)
            server = query.get("server", [""])[0].strip()
            port_text = query.get("port", [""])[0].strip()
            secret = query.get("secret", [""])[0].strip()
            port = int(port_text)
        except (ValueError, TypeError):
            continue
        if not server or not secret or not (1 <= port <= 65535):
            continue
        normalized = "tg://proxy?" + urlencode(
            [("server", server), ("port", str(port)), ("secret", secret)]
        )
        if normalized not in seen:
            configs.append(normalized)
            seen.add(normalized)
    return configs


def now_tehran() -> datetime:
    return datetime.now(TEHRAN)


def make_filename(moment: datetime) -> str:
    return f"mtproto-proxies-{moment.astimezone(TEHRAN):%Y-%m-%d_%H-%M-%S}.txt"


async def collect(channel_ids: list[str], api_id: int, api_hash: str, session: str,
                  lookback_hours: int) -> list[str]:
    client = TelegramClient(StringSession(session), api_id, api_hash)
    configs: list[str] = []
    seen: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("TELEGRAM_SESSION is not authorized; generate a fresh StringSession locally.")
        for channel_id in channel_ids:
            entity_arg: int | str
            try:
                entity_arg = int(channel_id)
            except ValueError:
                entity_arg = channel_id
            entity = await client.get_entity(entity_arg)
            # Read newest-first until the 12-hour cutoff; do not cap the message count,
            # so busy channels are still fully covered for the requested time window.
            async for message in client.iter_messages(entity):
                if message.date and message.date < cutoff:
                    break
                for config in extract_proxy_configs(message.message or ""):
                    if config not in seen:
                        configs.append(config)
                        seen.add(config)
    finally:
        await client.disconnect()
    return configs


def upload_file(path: Path) -> None:
    cookie = os.environ["UPLOADKON_COOKIE"].strip()
    xsrf = os.environ["UPLOADKON_XSRF_TOKEN"].strip()
    url = os.getenv("UPLOADKON_URL") or DEFAULT_UPLOAD_URL
    fields = {
        "clientMime": "text/plain",
        "clientExtension": "txt",
        "uploadType": os.getenv("UPLOADKON_UPLOAD_TYPE") or "bedrive",
        "backendId": os.getenv("UPLOADKON_BACKEND_ID") or "cdc_W8Ekm8",
        "parentId": os.getenv("UPLOADKON_PARENT_ID") or "",
        "workspaceId": os.getenv("UPLOADKON_WORKSPACE_ID") or "0",
        "relativePath": "",
    }
    headers = {
        "Cookie": cookie,
        "X-XSRF-TOKEN": xsrf,
        "Accept": "application/json",
        "Origin": "https://my.uploadkon.ir",
        "Referer": "https://my.uploadkon.ir/drive",
        "User-Agent": "mtproto-upload GitHub Actions",
    }
    with path.open("rb") as handle:
        response = requests.post(
            url,
            headers=headers,
            data=fields,
            files={"file": (path.name, handle, "text/plain")},
            timeout=90,
        )
    response.raise_for_status()
    try:
        result = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("Upload endpoint did not return JSON") from exc
    if not isinstance(result, (dict, list)):
        raise RuntimeError("Upload endpoint returned an unexpected response")
    print("Upload completed successfully.")


def main() -> int:
    channel_ids = [
        item.strip()
        for item in re.split(r"[,\s]+", os.getenv("CHANNEL_IDS", ""))
        if item.strip()
    ]
    if not channel_ids:
        raise RuntimeError("Set CHANNEL_IDS in GitHub Actions variables (comma-separated).")

    api_id = int(os.environ["TELEGRAM_API_ID"].strip())
    api_hash = os.environ["TELEGRAM_API_HASH"].strip()
    session = os.environ["TELEGRAM_SESSION"].strip()
    lookback = int(os.getenv("LOOKBACK_HOURS", str(DEFAULT_LOOKBACK_HOURS)).strip())
    if lookback < 1:
        raise ValueError("LOOKBACK_HOURS must be a positive integer")

    moment = now_tehran()
    output = Path(make_filename(moment))
    import asyncio
    configs = asyncio.run(collect(channel_ids, api_id, api_hash, session, lookback))
    output.write_text("\n".join(configs) + ("\n" if configs else ""), encoding="utf-8")
    msg = f"Wrote {len(configs)} unique MTProto proxy config(s) to {output.name}."
    print(msg)
    print(f"::notice::{msg}")
    if not configs:
        print("No valid proxy links were found; skipping the upload.")
        return 0
    upload_file(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Keep diagnostics useful without exposing credential values.
        err_msg = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")
        print(f"ERROR: {err_msg}", file=sys.stderr)
        print(f"::error::{err_msg}", file=sys.stderr)
        raise SystemExit(1)
