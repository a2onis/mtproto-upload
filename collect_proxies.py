#!/usr/bin/env python3
"""Collect Telegram MTProto proxy links and save them into the repository.

Results are written to ``results/mtproto-proxies-YYYY-MM-DD_HH-MM-SS.txt``,
timestamped in Asia/Tehran, and committed back to this repository by the GitHub
Actions workflow. Nothing is uploaded anywhere.
"""
from __future__ import annotations

import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:  # pragma: no cover
    TelegramClient = None  # type: ignore[assignment,misc]
    StringSession = None  # type: ignore[assignment,misc]

TEHRAN = ZoneInfo("Asia/Tehran")
# The job runs once a day, so look back a full day to avoid gaps.
DEFAULT_LOOKBACK_HOURS = 24
RESULTS_DIR = "results"
MAX_WEB_PAGES_PER_CHANNEL = 25
# Telegram proxy links are commonly shared as tg://proxy or t.me/proxy URLs.
LINK_RE = re.compile(
    r"(?:tg://proxy|(?:https?://)?(?:t\.me|telegram\.me)/proxy)\?[^\s<>\"'`]+",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = ").,;!?]}»'\""
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,}$")
MESSAGE_BLOCK_SPLIT_RE = re.compile(r'(?=<div[^>]+class="[^"]*tgme_widget_message\b)', re.IGNORECASE)
DATA_POST_RE = re.compile(r'\bdata-post="[^"/]+/(\d+)"', re.IGNORECASE)
DATETIME_RE = re.compile(r'<time[^>]+datetime="([^"]+)"', re.IGNORECASE)


def extract_proxy_configs(text: str) -> list[str]:
    """Return canonical tg://proxy links containing server, port and secret."""
    configs: list[str] = []
    seen: set[str] = set()
    unescaped = html.unescape(text or "")
    for match in LINK_RE.finditer(unescaped):
        raw = match.group(0).rstrip(TRAILING_PUNCTUATION)
        if "://" not in raw:
            raw = "https://" + raw
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


def normalize_channel_username(channel_id: str) -> str | None:
    """Extract a public Telegram channel username from @name, name, or t.me URL."""
    cleaned = channel_id.strip()
    if not cleaned:
        return None
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].strip()
    elif re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/", cleaned, re.IGNORECASE):
        if "://" not in cleaned:
            cleaned = "https://" + cleaned
        parts = [part for part in urlsplit(cleaned).path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() == "s":
            cleaned = parts[1]
        elif parts:
            cleaned = parts[0]
        else:
            return None
    if USERNAME_RE.match(cleaned):
        return cleaned
    return None


def parse_web_preview_page(page_html: str, cutoff: datetime) -> tuple[list[str], int | None, bool]:
    """Parse one t.me/s/<channel> page newest-first.

    Returns (configs, oldest_msg_id_on_page, reached_cutoff).
    """
    blocks = [
        block
        for block in MESSAGE_BLOCK_SPLIT_RE.split(page_html or "")
        if "tgme_widget_message" in block and "data-post=" in block
    ]
    if not blocks:
        return [], None, True

    configs: list[str] = []
    seen: set[str] = set()
    oldest_id: int | None = None
    reached_cutoff = False

    # t.me/s renders oldest at the top and newest at the bottom of each page.
    for block in reversed(blocks):
        post_match = DATA_POST_RE.search(block)
        if post_match:
            msg_id = int(post_match.group(1))
            if oldest_id is None or msg_id < oldest_id:
                oldest_id = msg_id

        time_match = DATETIME_RE.search(block)
        if time_match:
            try:
                msg_dt = datetime.fromisoformat(time_match.group(1))
                if msg_dt.tzinfo is None:
                    msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                if msg_dt < cutoff:
                    reached_cutoff = True
                    break
            except ValueError:
                pass

        for config in extract_proxy_configs(block):
            if config not in seen:
                configs.append(config)
                seen.add(config)

    return configs, oldest_id, reached_cutoff


def collect_channel_web_preview(username: str, cutoff: datetime,
                                session: requests.Session | None = None) -> list[str]:
    """Collect MTProto proxy configs from https://t.me/s/<username> within cutoff."""
    http = session or requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    configs: list[str] = []
    seen: set[str] = set()
    before_id: int | None = None

    for _ in range(MAX_WEB_PAGES_PER_CHANNEL):
        url = f"https://t.me/s/{username}"
        params = {"before": str(before_id)} if before_id is not None else None
        response = http.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        page_configs, oldest_id, reached_cutoff = parse_web_preview_page(response.text, cutoff)
        for config in page_configs:
            if config not in seen:
                configs.append(config)
                seen.add(config)
        if reached_cutoff or oldest_id is None or (before_id is not None and oldest_id >= before_id):
            break
        before_id = oldest_id

    return configs


def _extract_message_texts(message: object) -> str:
    parts: list[str] = [getattr(message, "message", None) or ""]
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            parts.append(str(url))
    reply_markup = getattr(message, "reply_markup", None)
    for row in getattr(reply_markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            url = getattr(button, "url", None)
            if url:
                parts.append(str(url))
    return "\n".join(part for part in parts if part)


def now_tehran() -> datetime:
    return datetime.now(TEHRAN)


def make_filename(moment: datetime) -> str:
    return f"mtproto-proxies-{moment.astimezone(TEHRAN):%Y-%m-%d_%H-%M-%S}.txt"


async def collect(channel_ids: list[str], api_id: int | None, api_hash: str, session: str,
                  lookback_hours: int) -> list[str]:
    configs: list[str] = []
    seen: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    remaining_channels = list(channel_ids)
    if api_id and api_hash and session:
        client: TelegramClient | None = None
        try:
            client = TelegramClient(StringSession(session), api_id, api_hash)
            await client.connect()
            if await client.is_user_authorized():
                unresolved: list[str] = []
                for channel_id in remaining_channels:
                    username = normalize_channel_username(channel_id)
                    entity_arg: int | str
                    if username is not None:
                        entity_arg = username
                    else:
                        try:
                            entity_arg = int(channel_id)
                        except ValueError:
                            entity_arg = channel_id
                    try:
                        entity = await client.get_entity(entity_arg)
                        # Read newest-first until the 12-hour cutoff; do not cap the message count,
                        # so busy channels are still fully covered for the requested time window.
                        async for message in client.iter_messages(entity):
                            if message.date and message.date < cutoff:
                                break
                            for config in extract_proxy_configs(_extract_message_texts(message)):
                                if config not in seen:
                                    configs.append(config)
                                    seen.add(config)
                    except Exception as exc:
                        if username is not None:
                            print(
                                f"Telethon lookup failed for {channel_id} ({exc}); "
                                "falling back to public web preview."
                            )
                            unresolved.append(channel_id)
                        else:
                            raise
                remaining_channels = unresolved
            else:
                print(
                    "TELEGRAM_SESSION is not authorized; falling back to public "
                    "https://t.me/s/ web preview for public channels."
                )
        except Exception as exc:
            print(
                f"Telethon session unavailable ({type(exc).__name__}: {exc}); "
                "falling back to public https://t.me/s/ web preview."
            )
        finally:
            if client is not None:
                await client.disconnect()

    if remaining_channels:
        http = requests.Session()
        for channel_id in remaining_channels:
            username = normalize_channel_username(channel_id)
            if username is None:
                raise RuntimeError(
                    f"Channel '{channel_id}' is not a public username and TELEGRAM_SESSION is not authorized."
                )
            for config in collect_channel_web_preview(username, cutoff, session=http):
                if config not in seen:
                    configs.append(config)
                    seen.add(config)

    return configs

def results_path(moment: datetime, directory: str | None = None) -> Path:
    """Where this run's results belong: ``results/<date>_<time>.txt``."""
    folder = Path(os.getenv("RESULTS_DIR") or directory or RESULTS_DIR)
    return folder / make_filename(moment)


def save_results(configs: list[str], path: Path) -> Path:
    """Write the collected configs to ``path``, creating the folder if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(configs) + ("\n" if configs else ""), encoding="utf-8")
    return path


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def main() -> int:
    channel_ids = [
        item.strip()
        for item in re.split(r"[,\s]+", os.getenv("CHANNEL_IDS", ""))
        if item.strip()
    ]
    if not channel_ids:
        raise RuntimeError("Set CHANNEL_IDS in GitHub Actions variables (comma-separated).")

    raw_api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_id = int(raw_api_id) if raw_api_id else None
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    session = os.getenv("TELEGRAM_SESSION", "").strip()
    lookback = _positive_int_env("LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS)

    # Stamp the run once, so the filename records when the results were received.
    moment = now_tehran()
    output = results_path(moment)

    import asyncio
    configs = asyncio.run(collect(channel_ids, api_id, api_hash, session, lookback))

    if not configs:
        msg = (
            f"No valid proxy links were found in the last {lookback}h across "
            f"{len(channel_ids)} channel(s); nothing written to {output.parent}/."
        )
        print(msg)
        print(f"::notice::{msg}")
        return 0

    save_results(configs, output)
    msg = (
        f"Wrote {len(configs)} unique MTProto proxy config(s) from the last "
        f"{lookback}h to {output}."
    )
    print(msg)
    print(f"::notice::{msg}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")
        print(f"ERROR: {err_msg}", file=sys.stderr)
        print(f"::error::{err_msg}", file=sys.stderr)
        raise SystemExit(1)
