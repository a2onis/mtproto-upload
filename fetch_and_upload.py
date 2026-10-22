#!/usr/bin/env python3
"""Collect Telegram MTProto proxy links and upload them to UploadKon."""
from __future__ import annotations

import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
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

if TYPE_CHECKING:
    import requests as requests_types

TEHRAN = ZoneInfo("Asia/Tehran")
DEFAULT_UPLOAD_URL = "https://my.uploadkon.ir/api/v1/file-entries"
DEFAULT_LOOKBACK_HOURS = 12
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


def _extract_xsrf_from_cookie(cookie: str) -> str:
    match = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", cookie)
    return unquote(match.group(1).strip()) if match else ""


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                jar[k] = v
    return jar


def _post_upload(
    url: str,
    path: Path,
    fields: dict[str, str],
    headers: dict[str, str],
) -> "requests_types.Response":
    req_headers = {**headers, "Connection": "close"}
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            with path.open("rb") as handle:
                return requests.post(
                    url,
                    headers=req_headers,
                    data=fields,
                    files={"file": (path.name, handle, "text/plain")},
                    timeout=90,
                )
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _extract_csrf_from_html(html_text: str) -> str:
    meta_match = re.search(
        r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
        html_text or "",
        re.IGNORECASE,
    )
    if meta_match:
        return meta_match.group(1).strip()
    json_match = re.search(r'"csrf_token"\s*:\s*"([^"]+)"', html_text or "")
    if json_match:
        return json_match.group(1).strip()
    b64_match = re.search(
        r'window\.bootstrapData\s*=\s*["\']([A-Za-z0-9+/=]+)["\']',
        html_text or "",
    )
    if b64_match:
        try:
            import base64
            decoded = base64.b64decode(b64_match.group(1)).decode("utf-8", errors="ignore")
            inner = re.search(r'"csrf_token"\s*:\s*"([^"]+)"', decoded)
            if inner:
                return inner.group(1).strip()
        except Exception:
            pass
    return ""


def upload_file(path: Path) -> None:
    cookie = os.environ["UPLOADKON_COOKIE"].strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    raw_xsrf = os.getenv("UPLOADKON_XSRF_TOKEN", "").strip()
    if raw_xsrf.lower().startswith("x-xsrf-token:"):
        raw_xsrf = raw_xsrf.split(":", 1)[1].strip()

    cookie_dict = _parse_cookie_header(cookie)
    unquoted_raw_xsrf = unquote(raw_xsrf)
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
    base_headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://my.uploadkon.ir",
        "Referer": "https://my.uploadkon.ir/drive",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
    }

    # Bootstrap/refresh session and XSRF-TOKEN from /drive first (essential when
    # UPLOADKON_COOKIE only contains a long-lived remember_web_* cookie or when
    # the 2-hour laravel_session has rotated).
    csrf_token = ""
    try:
        drive_resp = requests.get(
            "https://my.uploadkon.ir/drive",
            headers={
                "Cookie": cookie,
                "User-Agent": base_headers["User-Agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            allow_redirects=False,
            timeout=30,
        )
        new_cookies = drive_resp.cookies.get_dict()
        cookie_dict.update(new_cookies)
        csrf_token = _extract_csrf_from_html(drive_resp.text or "")
        print(
            f"::notice::UploadKon bootstrap /drive status={drive_resp.status_code}, "
            f"set_cookie_keys={sorted(new_cookies.keys())}, "
            f"merged_cookie_keys={sorted(cookie_dict.keys())}, "
            f"csrf_in_html={bool(csrf_token)}"
        )
    except Exception as exc:
        print(f"::notice::UploadKon bootstrap /drive skipped ({type(exc).__name__}: {exc})")

    merged_cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    active_xsrf = unquote(cookie_dict.get("XSRF-TOKEN", "") or "") or unquoted_raw_xsrf

    headers = {**base_headers, "Cookie": merged_cookie_str}
    if active_xsrf:
        headers["X-XSRF-TOKEN"] = active_xsrf
    if csrf_token:
        headers["X-CSRF-TOKEN"] = csrf_token

    post_fields = {**fields, **({"_token": csrf_token} if csrf_token else {})}
    with path.open("rb") as handle:
        response = requests.post(
            url,
            headers=headers,
            data=post_fields,
            files={"file": (path.name, handle, "text/plain")},
            timeout=90,
        )

    if not response.ok:
        snippet = response.text[:200].replace("\r", " ").replace("\n", " ")
        raise RuntimeError(f"UploadKon HTTP {response.status_code}: {snippet}")
    try:
        result = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("Upload endpoint did not return JSON") from exc
    if not isinstance(result, (dict, list)):
        raise RuntimeError("Upload endpoint returned an unexpected response")
    print("Upload completed successfully.")
    print("::notice::Upload to UploadKon completed successfully.")
    try:
        result = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("Upload endpoint did not return JSON") from exc
    if not isinstance(result, (dict, list)):
        raise RuntimeError("Upload endpoint returned an unexpected response")
    print("Upload completed successfully.")
    print("::notice::Upload to UploadKon completed successfully.")


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
