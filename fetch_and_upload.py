#!/usr/bin/env python3
"""Collect Telegram MTProto proxy links and upload them to UploadKon."""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
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
# Public anonymous uploader (no login); returns a direct file link.
DEFAULT_PUBLIC_UPLOAD_URL = "https://uploadkon.ir/"
DEFAULT_LOOKBACK_HOURS = 12
# Uploading to UploadKon can be slow: retry several times before giving up.
DEFAULT_UPLOAD_ATTEMPTS = 4
DEFAULT_UPLOAD_TIMEOUT = 120.0
UPLOAD_CONNECT_TIMEOUT = 30.0
UPLOAD_BACKOFF_SECONDS = 5.0
UPLOAD_BACKOFF_MAX_SECONDS = 30.0
# Overall budget for all endpoints/attempts so retries cannot outlast the CI job.
DEFAULT_TOTAL_UPLOAD_TIMEOUT = 600.0
MIN_ATTEMPT_SECONDS = 10.0
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
AUTH_HTTP_STATUS = frozenset({401, 403, 419})
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


def _strip_header_prefix(value: str, prefix: str) -> str:
    """Accept values pasted as a full header line, e.g. ``Cookie: a=b``."""
    value = (value or "").strip()
    if value.lower().startswith(prefix.lower()):
        value = value.split(":", 1)[1].strip()
    return value


def _one_line(value: object, limit: int = 300) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1.0, float(raw))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


class UploadError(RuntimeError):
    """Base class for UploadKon upload failures."""


class PermanentUploadError(UploadError):
    """Failure that retrying cannot fix (bad configuration or request)."""


class RetryableUploadError(UploadError):
    """Failure that may succeed on a later attempt."""


def _configure_tcp_mss() -> None:
    """Clamp TCP MSS to 1000 bytes to avoid PMTU blackholes on routes to .ir hosts."""
    import socket

    from urllib3.connection import HTTPConnection

    if not hasattr(socket, "TCP_MAXSEG"):
        return
    mss_opt = (socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1000)
    if mss_opt not in HTTPConnection.default_socket_options:
        HTTPConnection.default_socket_options = [
            *HTTPConnection.default_socket_options,
            mss_opt,
        ]


def _upload_url() -> str:
    return os.getenv("UPLOADKON_URL") or DEFAULT_UPLOAD_URL


def _upload_origin() -> str:
    """Origin of the configured upload endpoint (defaults to my.uploadkon.ir)."""
    parts = urlsplit(_upload_url())
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return "https://my.uploadkon.ir"


def _upload_base_headers() -> dict[str, str]:
    origin = _upload_origin()
    return {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": f"{origin}/drive",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
    }


def _upload_form_fields() -> dict[str, str]:
    return {
        "clientMime": "text/plain",
        "clientExtension": "txt",
        "uploadType": os.getenv("UPLOADKON_UPLOAD_TYPE") or "bedrive",
        "backendId": os.getenv("UPLOADKON_BACKEND_ID") or "cdc_W8Ekm8",
        "parentId": os.getenv("UPLOADKON_PARENT_ID") or "",
        "workspaceId": os.getenv("UPLOADKON_WORKSPACE_ID") or "0",
        "relativePath": "",
    }


def _load_upload_credentials() -> tuple[str, str]:
    cookie = _strip_header_prefix(os.getenv("UPLOADKON_COOKIE", ""), "cookie:")
    if not cookie:
        raise PermanentUploadError(
            "UPLOADKON_COOKIE is not set; cannot upload to UploadKon. Add the secret in repository settings."
        )
    xsrf = _strip_header_prefix(os.getenv("UPLOADKON_XSRF_TOKEN", ""), "x-xsrf-token:")
    return cookie, xsrf


def _refresh_upload_session(session: requests.Session, cookie: str, raw_xsrf: str,
                            base_headers: dict[str, str], timeout: float) -> tuple[str, str]:
    """Bootstrap/refresh the active cookie and XSRF token before uploading."""
    cookie_dict = _parse_cookie_header(cookie)
    try:
        response = session.get(
            f"{_upload_origin()}/api/v1/drive/file-entries",
            params={"perPage": 1},
            headers={**base_headers, "Cookie": cookie, "Connection": "close"},
            timeout=(min(UPLOAD_CONNECT_TIMEOUT, timeout), min(timeout, 60.0)),
        )
    except requests.exceptions.RequestException as exc:
        # A slow session endpoint must not block the upload itself: fall back to
        # the configured cookie/token and let the POST decide.
        print(
            f"Warning: could not refresh the UploadKon session "
            f"({type(exc).__name__}: {_one_line(exc)}); using the configured cookie.",
            file=sys.stderr,
        )
        active_xsrf = unquote(cookie_dict.get("XSRF-TOKEN", "") or "") or unquote(raw_xsrf)
        return cookie, active_xsrf

    if response.status_code in AUTH_HTTP_STATUS:
        raise RetryableUploadError(
            f"UploadKon rejected UPLOADKON_COOKIE (HTTP {response.status_code}); "
            "refresh the UPLOADKON_COOKIE and UPLOADKON_XSRF_TOKEN secrets"
        )
    if response.ok:
        cookie_dict.update(response.cookies.get_dict())
    else:
        print(
            f"Warning: UploadKon session refresh returned HTTP {response.status_code}; "
            "continuing with the configured cookie.",
            file=sys.stderr,
        )

    active_xsrf = unquote(cookie_dict.get("XSRF-TOKEN", "") or "") or unquote(raw_xsrf)
    # Laravel only needs aplod_kn_session in Cookie; keep the header compact.
    if "aplod_kn_session" in cookie_dict:
        cookie_str = f"aplod_kn_session={cookie_dict['aplod_kn_session']}"
    else:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    return cookie_str, active_xsrf


def _check_upload_response(response: requests.Response) -> dict | list:
    status = response.status_code
    snippet = _one_line(response.text[:300])
    if status in AUTH_HTTP_STATUS:
        raise RetryableUploadError(
            f"UploadKon HTTP {status} (session/CSRF rejected): {snippet}"
        )
    if status in RETRYABLE_HTTP_STATUS:
        raise RetryableUploadError(f"UploadKon HTTP {status}: {snippet}")
    if not response.ok:
        raise PermanentUploadError(f"UploadKon HTTP {status}: {snippet}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RetryableUploadError(
            f"upload endpoint did not return JSON: {snippet}"
        ) from exc
    if not isinstance(result, (dict, list)):
        raise RetryableUploadError("upload endpoint returned an unexpected response")
    return result


UPLOAD_LINK_KEYS = (
    "url",
    "link",
    "downloadUrl",
    "download_url",
    "publicUrl",
    "public_url",
    "fileUrl",
    "file_url",
    "shortUrl",
    "short_url",
)


def _find_upload_link(payload: object, depth: int = 0) -> str | None:
    """Best-effort lookup of the uploaded file's public URL in the API response."""
    if depth > 4:
        return None
    if isinstance(payload, dict):
        for key in UPLOAD_LINK_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        for value in payload.values():
            found = _find_upload_link(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload[:5]:
            found = _find_upload_link(item, depth + 1)
            if found:
                return found
    return None


class _TextareaParser(HTMLParser):
    """Collect ``<textarea id=...>value</textarea>`` pairs from UploadKon HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current_id: str | None = None
        self._buffer: list[str] = []
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "textarea":
            return
        attr_map = {name.lower(): value for name, value in attrs}
        textarea_id = attr_map.get("id")
        if textarea_id:
            self._current_id = textarea_id
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._current_id is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "textarea" and self._current_id is not None:
            self.values[self._current_id] = "".join(self._buffer).strip()
            self._current_id = None
            self._buffer = []


def extract_textarea_values(html_fragment: str) -> dict[str, str]:
    """Return ``{textarea id: value}`` from a fragment of UploadKon HTML."""
    parser = _TextareaParser()
    parser.feed(html_fragment or "")
    parser.close()
    return {key: html.unescape(value) for key, value in parser.values.items()}


def parse_public_upload_response(response_text: str) -> dict[str, str]:
    """Parse the anonymous uploadkon.ir AJAX reply into direct/delete links.

    The endpoint answers with JSON whose ``message_content`` (or ``i``) field holds
    HTML containing ``<textarea id="file1">URL</textarea>`` and ``#delCode``.
    """
    snippet = _one_line(response_text[:300])
    try:
        payload = json.loads(response_text)
    except (TypeError, ValueError) as exc:
        raise RetryableUploadError(
            f"uploadkon.ir did not return JSON: {snippet}"
        ) from exc

    items = payload if isinstance(payload, list) else [payload]
    fragment = ""
    for item in items:
        if isinstance(item, dict):
            fragment = item.get("message_content") or item.get("i") or ""
            if fragment:
                break
    if not isinstance(fragment, str) or not fragment.strip():
        # A rejection (size limit, banned extension, ...) has no link section.
        raise PermanentUploadError(
            f"uploadkon.ir rejected the upload: {snippet}"
        )

    values = extract_textarea_values(fragment)
    direct_url = values.get("file1", "").strip()
    if not direct_url.startswith(("http://", "https://")):
        match = re.search(r"https?://[^\s<>\"']+", html.unescape(fragment))
        direct_url = match.group(0) if match else ""
    if not direct_url:
        raise RetryableUploadError(
            f"uploadkon.ir response did not contain a file link: {snippet}"
        )
    return {"direct_url": direct_url, "delete_code": values.get("delCode", "").strip()}


def _public_upload_url() -> str:
    return os.getenv("UPLOADKON_PUBLIC_URL") or DEFAULT_PUBLIC_UPLOAD_URL


def _public_upload_headers() -> dict[str, str]:
    url = _public_upload_url()
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": url,
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
    }


def _public_upload_once(path: Path, timeout: float) -> dict[str, str]:
    """Upload ``path`` to the public uploadkon.ir endpoint (no login required)."""
    url = _public_upload_url()
    session = requests.Session()
    try:
        # Reopen the file for every attempt so a retry always sends the full body.
        with path.open("rb") as handle:
            response = session.post(
                url,
                headers=_public_upload_headers(),
                data={"submitr": "1", "ajax": "1"},
                files={"file": (path.name, handle, "text/plain")},
                timeout=(min(UPLOAD_CONNECT_TIMEOUT, timeout), timeout),
            )
        status = response.status_code
        snippet = _one_line(response.text[:300])
        if status in RETRYABLE_HTTP_STATUS:
            raise RetryableUploadError(f"uploadkon.ir HTTP {status}: {snippet}")
        if status in AUTH_HTTP_STATUS:
            raise RetryableUploadError(
                f"uploadkon.ir HTTP {status} (blocked or requires a cookie): {snippet}"
            )
        if not response.ok:
            raise PermanentUploadError(f"uploadkon.ir HTTP {status}: {snippet}")
        return parse_public_upload_response(response.text)
    finally:
        session.close()


def _upload_once(path: Path, timeout: float) -> dict | list:
    """One authenticated UploadKon drive upload attempt (session + POST)."""
    cookie, raw_xsrf = _load_upload_credentials()
    base_headers = _upload_base_headers()
    url = _upload_url()

    session = requests.Session()
    try:
        active_cookie, active_xsrf = _refresh_upload_session(
            session, cookie, raw_xsrf, base_headers, timeout
        )
        headers = {
            **base_headers,
            "Cookie": active_cookie,
            "X-XSRF-TOKEN": active_xsrf,
            "Connection": "close",
        }
        # Reopen the file for every attempt so a retry always sends the full body.
        with path.open("rb") as handle:
            response = session.post(
                url,
                headers=headers,
                data=_upload_form_fields(),
                files={"file": (path.name, handle, "text/plain")},
                timeout=(min(UPLOAD_CONNECT_TIMEOUT, timeout), timeout),
            )
        return _check_upload_response(response)
    finally:
        session.close()


UPLOAD_TARGETS = ("public", "drive")
DEFAULT_UPLOAD_MODE = "auto"


def _upload_targets(mode: str, cookie_available: bool) -> list[str]:
    """Return the ordered list of upload targets to try for ``mode``.

    ``auto`` sends the file to the public uploadkon.ir endpoint first (fast, no
    login) and falls back to the authenticated drive API when configured.
    """
    mode = (mode or DEFAULT_UPLOAD_MODE).strip().lower()
    if mode in ("public", "direct"):
        return ["public"]
    if mode in ("drive", "account", "my"):
        return ["drive"]
    if mode == DEFAULT_UPLOAD_MODE:
        targets = ["public"]
        if cookie_available:
            targets.append("drive")
        return targets
    raise PermanentUploadError(
        f"UPLOADKON_MODE must be one of {', '.join((*UPLOAD_TARGETS, DEFAULT_UPLOAD_MODE))}, "
        f"got {mode!r}"
    )


def _upload_target_once(target: str, path: Path, timeout: float) -> object:
    return _public_upload_once(path, timeout) if target == "public" else _upload_once(path, timeout)


def _report_upload_success(target: str, result: object) -> None:
    print(f"Upload to UploadKon ({target}) completed successfully.")
    if target == "public" and isinstance(result, dict):
        link = result.get("direct_url", "")
        delete_code = result.get("delete_code", "")
        if link:
            print(f"::notice::Uploaded file: {link}")
            print(f"Direct link: {link}")
        if delete_code:
            print(f"Delete code: {delete_code}")
        print("::notice::Upload to UploadKon completed successfully.")
        return

    print("::notice::Upload to UploadKon completed successfully.")
    link = _find_upload_link(result)
    if link:
        print(f"::notice::Uploaded file: {link}")


def _try_upload_target(target: str, path: Path, attempts: int, timeout: float,
                       deadline: float) -> None:
    """Upload via one target with retries; raises when every attempt fails."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining < MIN_ATTEMPT_SECONDS:
            last_error = last_error or RuntimeError("upload time budget exhausted")
            print(
                f"Stopping {target} upload attempts: only {remaining:.0f}s of the "
                "configured upload budget remain.",
                file=sys.stderr,
            )
            break
        try:
            result = _upload_target_once(target, path, min(timeout, remaining))
        except PermanentUploadError as exc:
            message = (
                f"Upload via {target} failed permanently: "
                f"{type(exc).__name__}: {_one_line(exc)}"
            )
            print(message, file=sys.stderr)
            print(f"::warning::{message}", file=sys.stderr)
            raise
        except (RetryableUploadError, OSError) as exc:
            # Timeouts/resets (requests raises IOError subclasses), 5xx, expired CSRF.
            last_error = exc
            message = (
                f"Upload attempt {attempt}/{attempts} via {target} failed: "
                f"{type(exc).__name__}: {_one_line(exc)}"
            )
            print(message, file=sys.stderr)
            print(f"::warning::{message}", file=sys.stderr)
            if attempt < attempts:
                delay = min(
                    UPLOAD_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                    UPLOAD_BACKOFF_MAX_SECONDS,
                )
                if deadline - time.monotonic() <= delay + MIN_ATTEMPT_SECONDS:
                    break
                print(f"Retrying the UploadKon upload in {delay:.0f}s ...")
                time.sleep(delay)
            continue

        _report_upload_success(target, result)
        return

    raise RuntimeError(
        f"{target} upload failed after {attempts} attempt(s): "
        f"{type(last_error).__name__}: {_one_line(last_error)}"
    )


def upload_file(path: Path) -> None:
    """Send ``path`` to UploadKon, retrying and falling back between endpoints."""
    attempts = _positive_int_env("UPLOADKON_MAX_ATTEMPTS", DEFAULT_UPLOAD_ATTEMPTS)
    timeout = _positive_float_env("UPLOADKON_TIMEOUT", DEFAULT_UPLOAD_TIMEOUT)
    mode = os.getenv("UPLOADKON_MODE", DEFAULT_UPLOAD_MODE)
    cookie_available = bool(_strip_header_prefix(os.getenv("UPLOADKON_COOKIE", ""), "cookie:"))
    targets = _upload_targets(mode, cookie_available)
    total_budget = _positive_float_env("UPLOADKON_TOTAL_TIMEOUT", DEFAULT_TOTAL_UPLOAD_TIMEOUT)
    deadline = time.monotonic() + total_budget
    _configure_tcp_mss()

    print(
        f"Uploading {path.name} to UploadKon via {' -> '.join(targets)} "
        f"(up to {attempts} attempt(s) per endpoint, {timeout:.0f}s read timeout each, "
        f"{total_budget:.0f}s total budget)."
    )

    failures: list[str] = []
    permanent_only = True
    for index, target in enumerate(targets):
        try:
            _try_upload_target(target, path, attempts, timeout, deadline)
        except UploadError as exc:
            permanent_only = permanent_only and isinstance(exc, PermanentUploadError)
            failures.append(f"{target}: {type(exc).__name__}: {_one_line(exc)}")
            if target == "public" and not os.getenv("UPLOADKON_PUBLIC_URL"):
                # The public endpoint is anonymous; a hard rejection here usually
                # means the host is blocking GitHub's runners.
                print(
                    "Public uploadkon.ir endpoint rejected the upload; "
                    "trying the next target if one is configured.",
                    file=sys.stderr,
                )
        except RuntimeError as exc:
            permanent_only = False
            failures.append(f"{target}: {_one_line(exc)}")
        else:
            return
        if index + 1 < len(targets):
            print(
                f"Falling back to the {targets[index + 1]} UploadKon endpoint ...",
                file=sys.stderr,
            )

    summary = "UploadKon upload failed for every endpoint: " + " | ".join(failures)
    if permanent_only:
        raise PermanentUploadError(summary)
    raise RuntimeError(summary)


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
