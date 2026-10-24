#!/usr/bin/env python3
"""Collect Telegram MTProto proxy links and upload them to UploadKon."""
from __future__ import annotations

import html
import io
import json
import os
import re
import secrets
import sys
import tempfile
import time
import zipfile
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
# Outgoing TCP MSS clamp; 0 disables it (see _configure_tcp_mss).
DEFAULT_TCP_MSS = 1000
# Fast transport-level retries for connections the host drops before answering.
# Cheap (an RST is instant) and it is the failure the UploadKon hosts produce.
DEFAULT_TRANSPORT_RETRIES = 3
UPLOAD_BACKOFF_SECONDS = 5.0
UPLOAD_BACKOFF_MAX_SECONDS = 30.0
# Overall budget for all endpoints/attempts so retries cannot outlast the CI job.
DEFAULT_TOTAL_UPLOAD_TIMEOUT = 600.0
MIN_ATTEMPT_SECONDS = 10.0
# Share of the remaining budget kept back for the next endpoint, so a host that
# ignores every request cannot starve the fallback endpoint entirely.
RESERVE_FOR_NEXT_ENDPOINT = 0.4
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


def _set_cookie_headers(response: object) -> list[str]:
    """Every ``Set-Cookie`` header of ``response``, in arrival order.

    ``response.cookies`` collapses repeated cookie names into a single value and
    only keeps cookies the cookie policy accepted, so read the raw headers to
    capture exactly what the server sent.
    """
    headers = getattr(response, "raw", None)
    headers = getattr(headers, "headers", None)
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        try:
            return [str(value) for value in getlist("Set-Cookie")]
        except Exception:  # pragma: no cover - exotic urllib3 backends
            pass
    headers = getattr(response, "headers", None)
    if headers is None:
        return []
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        try:
            return [str(value) for value in getlist("Set-Cookie")]
        except Exception:  # pragma: no cover - header objects without getlist
            return []
    if isinstance(headers, dict):
        value = headers.get("Set-Cookie") or headers.get("set-cookie")
        return [str(value)] if value else []
    return []


def _store_response_cookies(session: requests_types.Session, response: object,
                            url: str) -> dict[str, str]:
    """Keep the cookies of ``response`` so the next request can send them back.

    UploadKon (a Laravel app) rotates both ``XSRF-TOKEN`` and
    ``aplod_kn_session`` on *every* response, including the ones that fail, and
    only accepts the newest pair. Anything we do not store here is silently
    dropped, which is what turns the following POST into a ``419``.
    """
    stored: dict[str, str] = {}
    origin = urlsplit(url or "")
    host = origin.hostname or None
    for header in _set_cookie_headers(response):
        pairs = re.split(r";\s*", header, maxsplit=1)
        name, sep, value = (pairs[0] if pairs else "").partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        # The jar (not a Cookie header) is the single source of truth: requests
        # adds these cookies to every later request automatically.
        session.cookies.set(name, value.strip(), domain=host)
        stored[name] = value.strip()
    if not stored:
        jar = getattr(response, "cookies", None)
        if jar is not None:
            try:
                for name, value in dict(jar).items():
                    session.cookies.set(name, value, domain=host)
                    stored[name] = value
            except Exception:  # pragma: no cover - defensive
                pass
    if stored:
        print(
            "Stored cookie(s) from the server response for the next request: "
            + ", ".join(sorted(stored))  # names only; never log cookie values
        )
    return stored


def _jar_cookies(session: requests_types.Session) -> dict[str, str]:
    """Cookies the session already collected from previous responses."""
    jar = getattr(session, "cookies", None)
    if jar is None:
        return {}
    try:
        return {name: value or "" for name, value in dict(jar).items()}
    except Exception:  # pragma: no cover - defensive
        return {}


def _cookie_header(cookies: dict[str, str]) -> str:
    """Render a ``Cookie`` request header from the accumulated cookie set."""
    return "; ".join(
        f"{name}={value}" for name, value in cookies.items() if value != ""
    )


def _jar_size(session: object) -> int:
    """How many cookies the session already holds (0 when it has no real jar)."""
    jar = getattr(session, "cookies", None)
    if jar is None:
        return 0
    try:
        return len(jar)
    except TypeError:  # pragma: no cover - mocks without a sized jar
        return 0


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
    """Clamp the outgoing TCP MSS unless the host route needs full-size segments.

    A 1000-byte MSS avoids PMTU blackholes on some routes to `.ir` hosts, but it
    is not free: every segment carries more overhead and a middlebox that
    inspects MSS can treat the connection as unusual. Set
    ``UPLOADKON_TCP_MSS=0`` to leave the kernel default in place.
    """
    import socket

    from urllib3.connection import HTTPConnection

    if not hasattr(socket, "TCP_MAXSEG"):
        return
    raw = os.getenv("UPLOADKON_TCP_MSS", "").strip().lower()
    if raw in {"0", "off", "false", "no", "disable", "disabled"}:
        return
    try:
        mss = int(raw) if raw else DEFAULT_TCP_MSS
    except ValueError:
        raise ValueError(f"UPLOADKON_TCP_MSS must be an integer or 0, got {raw!r}")
    if mss < 1:
        return
    mss_opt = (socket.IPPROTO_TCP, socket.TCP_MAXSEG, mss)
    if mss_opt not in HTTPConnection.default_socket_options:
        HTTPConnection.default_socket_options = [
            *HTTPConnection.default_socket_options,
            mss_opt,
        ]


#: ``Connection: close`` forces a brand-new TCP+TLS handshake for every request.
#: Browsers keep the socket alive, and hosts that rate-limit handshakes are far
#: more likely to drop a fresh connection than a reused one, so keep-alive is the
#: default. Set ``UPLOADKON_CONNECTION_CLOSE=1`` to restore the old behaviour.
def _connection_headers() -> dict[str, str]:
    raw = os.getenv("UPLOADKON_CONNECTION_CLOSE", "").strip().lower()
    return {"Connection": "close"} if raw in {"1", "true", "yes", "on"} else {}


_EGRESS_IP: str | None = None


def _egress_ip(timeout: float = 8.0) -> str:
    """Best-effort public IP of this runner, so a block can be reported precisely."""
    global _EGRESS_IP
    if _EGRESS_IP:
        return _EGRESS_IP
    probe = requests.Session()
    try:
        _configure_session(probe)
        response = probe.get(
            os.getenv("UPLOADKON_EGRESS_IP_URL") or "https://api.ipify.org?format=json",
            headers={"Accept": "application/json",
                     "User-Agent": _public_upload_headers()["User-Agent"]},
            timeout=timeout,
        )
        try:
            ip = str(response.json().get("ip", "")).strip()
        except ValueError:
            ip = response.text.strip()[:64]
    except Exception:  # pragma: no cover - the probe must never break an upload
        ip = ""
    finally:
        probe.close()
    _EGRESS_IP = ip or "unknown"
    return _EGRESS_IP


def _describe_connection_failure(host: str, exc: Exception) -> str:
    """Describe a dropped connection without guessing at its cause.

    The actionable part comes first because the collected summary is truncated.
    Whether the host refuses *this runner* or only *this kind of request* is not
    something a failed request can tell us, so say what happened and point at the
    probe that can.
    """
    text = _one_line(exc)
    dropped = "RemoteDisconnected" in text or "Connection aborted" in text
    what = "closed the connection without sending any response"
    if not dropped:
        what = "could not be reached"
    return (
        f"{host} {what}. Runner egress IP: {_egress_ip()}. Run "
        "`python fetch_and_upload.py --diagnose-upload` to find out which request "
        "the host drops, and set UPLOADKON_PROXY to route around it if the host "
        "refuses this runner. "
        f"Detail: {text}"
    )


def _upload_url() -> str:
    return os.getenv("UPLOADKON_URL") or DEFAULT_UPLOAD_URL


def _upload_origin() -> str:
    """Origin of the configured upload endpoint (defaults to my.uploadkon.ir)."""
    parts = urlsplit(_upload_url())
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return "https://my.uploadkon.ir"


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"
)


def _upload_base_headers() -> dict[str, str]:
    """Headers for the my.uploadkon.ir drive API, mirroring the browser.

    A captured working upload sends no ``X-Requested-With`` and does send the
    ``Sec-Fetch-*`` triple, so match that instead of inventing an AJAX marker.
    """
    origin = _upload_origin()
    return {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": origin,
        "Referer": f"{origin}/drive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": os.getenv("UPLOADKON_USER_AGENT") or DEFAULT_USER_AGENT,
    }


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"
)


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


def _drive_listing_params() -> dict[str, str]:
    """The drive listing query the web UI issues, used to warm the session."""
    return {
        "section": "home",
        "folderId": os.getenv("UPLOADKON_PARENT_ID") or "0",
        "workspaceId": os.getenv("UPLOADKON_WORKSPACE_ID") or "0",
        "orderBy": "created_at",
        "orderDir": "desc",
        "page": "1",
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
                            base_headers: dict[str, str],
                            timeout: float) -> tuple[dict[str, str], str]:
    """Bootstrap/refresh the active cookies and XSRF token before uploading.

    Returns the cookie set to send next (configured cookies overwritten by
    whatever the server just sent back) plus the matching ``X-XSRF-TOKEN`` value.
    """
    # Cookies collected from earlier responses win over the configured secret:
    # the server has rotated them since, and only accepts the newest values.
    cookie_dict = _parse_cookie_header(cookie)
    cookie_dict.update(_jar_cookies(session))
    url = f"{_upload_origin()}/api/v1/drive/file-entries"
    try:
        response = session.get(
            url,
            params=_drive_listing_params(),
            headers={**base_headers, **_connection_headers(),
                     "Cookie": _cookie_header(cookie_dict)},
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
        return cookie_dict, active_xsrf

    # Whatever the server sent back must survive until the next request, even
    # when the response itself is an error: Laravel rotates XSRF-TOKEN and
    # aplod_kn_session on every reply and rejects the previous pair.
    cookie_dict.update(_store_response_cookies(session, response, url))

    if response.status_code in AUTH_HTTP_STATUS:
        raise RetryableUploadError(
            f"UploadKon rejected UPLOADKON_COOKIE (HTTP {response.status_code}); "
            "refresh the UPLOADKON_COOKIE and UPLOADKON_XSRF_TOKEN secrets"
        )
    if not response.ok:
        print(
            f"Warning: UploadKon session refresh returned HTTP {response.status_code}; "
            "continuing with the cookies received so far.",
            file=sys.stderr,
        )

    active_xsrf = unquote(cookie_dict.get("XSRF-TOKEN", "") or "") or unquote(raw_xsrf)
    return cookie_dict, active_xsrf


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
    """Best-effort lookup of the uploaded file's URL in the API response.

    The drive API answers ``201`` with ``{"fileEntry": {..., "url":
    "api/v1/file-entries/76344"}}`` - a relative path, so resolve it against the
    configured origin instead of discarding it.
    """
    if depth > 4:
        return None
    if isinstance(payload, dict):
        for key in UPLOAD_LINK_KEYS:
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            if value.startswith(("http://", "https://")):
                return value
            if key == "url":
                return f"{_upload_origin()}/{value.lstrip('/')}"
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


def _public_rejection_message(payload: object) -> str:
    """Extract the human-readable rejection text from an uploadkon.ir reply."""
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            continue
        fragment = item.get("i") or item.get("message_content") or ""
        if not isinstance(fragment, str):
            continue
        text = re.sub(r"<br\s*/?>", " ", fragment, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text[:300]
    return "no file link in the response"


def parse_public_upload_response(response_text: str) -> dict[str, str]:
    """Parse the anonymous uploadkon.ir AJAX reply into direct/delete links.

    The endpoint answers with JSON whose ``message_content`` (or ``i``) field holds
    HTML containing ``<textarea id="file1">URL</textarea>`` and a delete code.
    Rejections (for example an unsupported file extension) carry ``index_err``.
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
        raise PermanentUploadError(
            f"uploadkon.ir rejected the upload: {_public_rejection_message(payload)}"
        )

    values = extract_textarea_values(fragment)
    direct_url = values.get("file1", "").strip()
    if not direct_url.startswith(("http://", "https://")):
        match = re.search(r"https?://(?:www\.)?uploadkon\.ir/[^\s<>\"']+",
                          html.unescape(fragment))
        direct_url = match.group(0) if match else ""
    if not direct_url:
        raise PermanentUploadError(
            f"uploadkon.ir rejected the upload: {_public_rejection_message(payload)}"
        )
    delete_code = values.get("delCode", "").strip() or next(
        (value for key, value in values.items() if "del" in key.lower()), ""
    )
    return {"direct_url": direct_url, "delete_code": delete_code}


def build_archive(path: Path) -> tuple[bytes, str]:
    """Return zip bytes containing ``path`` and the archive filename.

    The anonymous uploadkon.ir endpoint rejects ``.txt``/``.csv``/``.log`` and other
    text extensions ("پسوند ... پشتیبانی نمیشود"), but accepts archives.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(path, arcname=path.name)
    return buffer.getvalue(), f"{path.name}.zip"


def _encode_multipart(fields: dict[str, str], field_name: str, filename: str,
                      payload: bytes, mime: str) -> tuple[bytes, str]:
    """Build a multipart body byte-compatible with the captured browser upload.

    Two details differ from what ``requests`` sends:

    * the ``file`` part comes **first**, ahead of ``clientMime``/``uploadType``/...;
    * plain form fields carry only ``Content-Disposition`` - no
      ``Content-Type: application/octet-stream``, which is what urllib3's encoder
      adds for a ``(None, value)`` tuple.
    """
    boundary = _multipart_boundary(payload, filename)
    crlf = b"\r\n"
    chunks: list[bytes] = []

    def add(disposition: str, content_type: str | None, value: bytes) -> None:
        chunks.append(b"--" + boundary + crlf)
        chunks.append(b"Content-Disposition: form-data; " + disposition.encode() + crlf)
        if content_type:
            chunks.append(b"Content-Type: " + content_type.encode() + crlf)
        chunks.append(crlf + value + crlf)

    add(f'name="{field_name}"; filename="{filename}"', mime, payload)
    for name, value in fields.items():
        add(f'name="{name}"', None, str(value).encode())
    chunks.append(b"--" + boundary + b"--" + crlf)
    return b"".join(chunks), f"multipart/form-data; boundary={boundary.decode()}"


def _multipart_boundary(payload: bytes, filename: str) -> bytes:
    """A boundary that cannot occur in the body, shaped like Firefox's."""
    while True:
        candidate = (
            b"----geckoformboundary" + secrets.token_hex(16).encode()
        )
        if candidate not in payload and candidate not in filename.encode():
            return candidate


def _public_archive_enabled() -> bool:
    raw = os.getenv("UPLOADKON_PUBLIC_ARCHIVE", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _public_upload_url() -> str:
    return os.getenv("UPLOADKON_PUBLIC_URL") or DEFAULT_PUBLIC_UPLOAD_URL


def _upload_host(url: str) -> str:
    """Human-readable host of ``url`` for log messages."""
    parts = urlsplit(url or "")
    return parts.netloc or parts.path or (url or "the upload host")


def _make_retry_policy(retries: int):
    """Retry only the failures that cannot have reached the server.

    ``('Connection aborted.', RemoteDisconnected(...))`` arrives as a urllib3
    ``ProtocolError``. urllib3 counts that as a *read* failure, so a plain
    ``Retry(read=0)`` never retries it - which is exactly the error the UploadKon
    hosts produce. We retry it here, but only while no response byte has been
    received; once the server has started answering, a resend could create a
    duplicate file, so that case is left to the outer attempt loop.
    """
    from urllib3.exceptions import MaxRetryError, ProtocolError
    from urllib3.util.retry import Retry, RequestHistory

    class DroppedConnectionRetry(Retry):
        def is_retry(self, method, status_code, has_retry_after=False):  # noqa: D102
            if method.upper() in self.allowed_methods:
                return True
            return super().is_retry(method, status_code, has_retry_after)

        def increment(self, method=None, url=None, response=None, error=None,
                      _pool=None, _stacktrace=None):
            if error is None or not isinstance(error, ProtocolError):
                return super().increment(method, url, response, error, _pool,
                                         _stacktrace)
            cause = error.args[1] if len(error.args) > 1 else None
            if not isinstance(
                cause, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
            ):
                # Not a dropped connection (e.g. a genuine read problem).
                raise error
            if response is not None and getattr(response, "closed", True) is False:
                # Bytes already came back: never resend an upload blindly.
                raise error
            # Mirror urllib3's own bookkeeping, including the "check exhausted
            # *before* decrementing" order: Retry.new() turns a 0 total into
            # False, which would mean "retry forever".
            if self.is_exhausted():
                raise MaxRetryError(_pool, url, reason=error) from error
            total = self.total
            if total is not False and total is not None:
                total -= 1
            new_retry = self.new(
                total=total,
                connect=self.connect,
                read=self.read,
                redirect=self.redirect,
                status=self.status,
                other=self.other,
                history=self.history + (RequestHistory(method, url, error, None, None),),
            )
            return new_retry

    try:
        return DroppedConnectionRetry(
            total=retries, connect=retries, read=0, status=0, redirect=0,
            backoff_factor=0.3,
            allowed_methods=frozenset({"GET", "HEAD", "POST"}),
            raise_on_status=False,
        )
    except TypeError:  # pragma: no cover - urllib3 < 1.26
        return DroppedConnectionRetry(
            total=retries, connect=retries, read=0, status=0, redirect=0,
            backoff_factor=0.3,
            method_whitelist=frozenset({"GET", "HEAD", "POST"}),
            raise_on_status=False,
        )


def _configure_session(session: requests.Session) -> None:
    """Apply optional proxy/cookie settings shared by both upload endpoints."""
    proxy = os.getenv("UPLOADKON_PROXY", "").strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    public_cookie = _strip_header_prefix(os.getenv("UPLOADKON_PUBLIC_COOKIE", ""), "cookie:")
    if public_cookie:
        session.cookies.update(_parse_cookie_header(public_cookie))
    retries = _transport_retries()
    if retries > 0:
        # A dropped connection costs milliseconds to retry on a fresh socket, so
        # do it below requests instead of burning an outer attempt (and its 5-30 s
        # backoff) on it.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4,
            pool_maxsize=4,
            max_retries=_make_retry_policy(retries),
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)


def _transport_retries() -> int:
    raw = os.getenv("UPLOADKON_TRANSPORT_RETRIES", "").strip()
    if not raw:
        return DEFAULT_TRANSPORT_RETRIES
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise ValueError(f"UPLOADKON_TRANSPORT_RETRIES must be an integer, got {raw!r}") from exc


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
        **_connection_headers(),
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
    }


def _warm_up_public_session(session: requests.Session, timeout: float) -> None:
    """Load the upload page first so any session/anti-bot cookie is present."""
    url = _public_upload_url()
    if _jar_size(session):
        # A previous attempt already collected the host's cookies; skip the extra
        # request and reuse the warm connection instead.
        print("uploadkon.ir session warm-up skipped: reusing the cookies already held.")
        return
    warm_headers = {
        key: value
        for key, value in _public_upload_headers().items()
        if key != "X-Requested-With"
    }
    warm_headers["Accept"] = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    )
    try:
        response = session.get(
            url,
            headers=warm_headers,
            timeout=(min(UPLOAD_CONNECT_TIMEOUT, timeout), min(timeout, 60.0)),
        )
    except requests.exceptions.RequestException as exc:
        print(
            f"Warning: uploadkon.ir page load failed ({type(exc).__name__}: "
            f"{_one_line(exc)}); continuing with the upload.",
            file=sys.stderr,
        )
        return
    print(
        f"uploadkon.ir session warm-up: HTTP {response.status_code}, "
        f"{len(session.cookies)} cookie(s)."
    )


def _public_upload_once(path: Path, timeout: float,
                        http: requests.Session | None = None) -> dict[str, str]:
    """Upload ``path`` to the public uploadkon.ir endpoint (no login required).

    ``http`` is reused across attempts so the cookies set by an earlier response
    are still available to the next request.
    """
    url = _public_upload_url()
    session = http or requests.Session()
    try:
        if http is None:
            _configure_session(session)
        _warm_up_public_session(session, timeout)
        headers = _public_upload_headers()
        if _public_archive_enabled():
            payload, upload_name = build_archive(path)
            mime = "application/zip"
        else:
            # Reopen the file for every attempt so a retry always sends the full body.
            payload = path.read_bytes()
            upload_name = path.name
            mime = "text/plain"
        print(
            f"Uploading {upload_name} ({len(payload)} bytes) to the public "
            "uploadkon.ir endpoint ..."
        )
        body, content_type = _encode_multipart(
            {"submitr": "1", "ajax": "1"}, "file", upload_name, payload, mime
        )
        response = session.post(
            url,
            headers={**headers, "Content-Type": content_type},
            data=body,
            timeout=(min(UPLOAD_CONNECT_TIMEOUT, timeout), timeout),
        )
        # Keep whatever the host sent back for the next attempt.
        _store_response_cookies(session, response, url)
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
    except requests.exceptions.ConnectionError as exc:
        raise RetryableUploadError(
            _describe_connection_failure(_upload_host(url), exc)
        ) from exc
    finally:
        if http is None:
            session.close()


def _upload_once(path: Path, timeout: float,
                 http: requests.Session | None = None) -> dict | list:
    """One authenticated UploadKon drive upload attempt (session + POST).

    ``http`` is reused across attempts so the cookies rotated by a previous
    response are still in the jar for the next request.
    """
    cookie, raw_xsrf = _load_upload_credentials()
    base_headers = _upload_base_headers()
    url = _upload_url()

    session = http or requests.Session()
    try:
        if http is None:
            _configure_session(session)
        active_cookies, active_xsrf = _refresh_upload_session(
            session, cookie, raw_xsrf, base_headers, timeout
        )
        headers = {
            **base_headers,
            # Send every cookie we hold: the session cookie the API authenticates
            # with plus the XSRF-TOKEN the server rotated in its last response.
            "Cookie": _cookie_header(active_cookies),
            **_connection_headers(),
        }
        if active_xsrf:
            headers["X-XSRF-TOKEN"] = active_xsrf
        print(
            f"UploadKon drive request carries {len(active_cookies)} cookie(s): "
            + ", ".join(sorted(active_cookies))  # names only; never log values
        )
        # Read the file for every attempt so a retry always sends the full body.
        body, content_type = _encode_multipart(
            _upload_form_fields(), "file", path.name, path.read_bytes(), "text/plain"
        )
        response = session.post(
            url,
            headers={**headers, "Content-Type": content_type},
            data=body,
            timeout=(min(UPLOAD_CONNECT_TIMEOUT, timeout), timeout),
        )
        # The POST rotates the cookies too; keep them for a possible retry.
        _store_response_cookies(session, response, url)
        return _check_upload_response(response)
    finally:
        if http is None:
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


def _upload_target_once(target: str, path: Path, timeout: float,
                        http: requests.Session | None = None) -> object:
    if target == "public":
        return _public_upload_once(path, timeout, http)
    return _upload_once(path, timeout, http)


DIAGNOSE_FILENAME = "mtproto-proxies-probe.txt"
# Filler for the probe's benign upload; harmless on its own and easy to spot.
BENIGN_PROBE_LINE = "tg://proxy?server=probe.example&port=443&secret=probe\n"


def diagnose_upload(timeout: float = 30.0, path: Path | None = None) -> bool:
    """Work out which request an upload host drops, and say so plainly.

    A failed upload cannot tell us whether the host refuses this runner, objects
    to the request shape, to the payload size, or to the file's contents. So send
    progressively closer approximations and report the first one that gets no
    answer. The upload cases form a 2x2 over size and content:

    1. plain GET of the upload page
    2. the same GET with the uploader's full header set
    3. a small form POST (no file)
    4. multipart upload, small + benign contents
    5. multipart upload, real size + benign contents
    6. multipart upload, small + the real contents
    7. multipart upload, the real file that failed
    8. drive listing GET, then the drive upload POST
    """
    public_url = _public_upload_url()
    drive_url = _upload_url()
    base = _public_upload_headers()
    page_headers = {
        key: value for key, value in base.items() if key != "X-Requested-With"
    }
    page_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    real_bytes = b""
    if path is not None:
        try:
            real_bytes = path.read_bytes()
        except OSError:
            real_bytes = b""
    benign_small = BENIGN_PROBE_LINE.encode()
    benign_large = _benign_payload_matching(real_bytes, benign_small)

    steps: list[tuple[str, dict]] = [
        (f"GET  {public_url} (plain page load)",
         {"method": "GET", "url": public_url, "headers": {"Accept": "*/*"}}),
        (f"GET  {public_url} (uploader headers + page cookies)",
         {"method": "GET", "url": public_url, "headers": page_headers}),
        (f"POST {public_url} (form only, no file)",
         {"method": "POST", "url": public_url, "headers": base,
          "data": {"submitr": "1", "ajax": "1"}}),
    ]
    # Order matters: benign first, then the real file, so a failure can be
    # compared against the benign upload of the same size.
    upload_cases: list[tuple[str, bytes]] = [
        ("small, benign contents", benign_small),
        (f"real size {len(benign_large)} bytes, benign contents", benign_large),
    ]
    if real_bytes:
        upload_cases.append((f"the real file ({len(real_bytes)} bytes)", real_bytes))

    with tempfile.TemporaryDirectory() as tmp:
        for label, contents in upload_cases:
            source = Path(tmp) / DIAGNOSE_FILENAME
            source.write_bytes(contents)
            if _public_archive_enabled():
                archive, archive_name = build_archive(source)
                mime = "application/zip"
            else:
                archive, archive_name, mime = contents, DIAGNOSE_FILENAME, "text/plain"
            body, content_type = _encode_multipart(
                {"submitr": "1", "ajax": "1"}, "file", archive_name, archive, mime
            )
            steps.append((
                f"POST {public_url} (multipart upload, {label})",
                {"method": "POST", "url": public_url,
                 "headers": {**base, "Content-Type": content_type}, "data": body},
            ))

    steps.append((
        f"GET  {drive_url} (drive listing, unauthenticated)",
        {"method": "GET", "url": drive_url, "params": _drive_listing_params(),
         "headers": _upload_base_headers()},
    ))
    probe_body, probe_type = _encode_multipart(
        _upload_form_fields(), "file", DIAGNOSE_FILENAME, benign_small, "text/plain"
    )
    steps.append((
        f"POST {drive_url} (drive upload POST, unauthenticated)",
        {"method": "POST", "url": drive_url,
         "headers": {**_upload_base_headers(), "Content-Type": probe_type},
         "data": probe_body},
    ))

    session = requests.Session()
    _configure_session(session)
    results: list[tuple[str, str]] = []
    print(f"Runner egress IP: {_egress_ip()}")
    try:
        for label, kwargs in steps:
            try:
                response = session.request(
                    timeout=(min(UPLOAD_CONNECT_TIMEOUT, timeout), timeout), **kwargs
                )
            except requests.exceptions.RequestException as exc:
                results.append((label, f"FAILED ({type(exc).__name__}: {_one_line(exc, 120)})"))
                break
            results.append((
                label,
                f"answered HTTP {response.status_code} ({len(response.content)} bytes)",
            ))
    finally:
        session.close()

    print("\nUpload host probe (first failure is the interesting one):")
    for label, outcome in results:
        print(f"  [{'ok  ' if outcome.startswith('answered') else 'FAIL'}] {label}\n         -> {outcome}")
    _explain_probe(results)
    return all(outcome.startswith("answered") for _, outcome in results)


def _benign_payload_matching(real_bytes: bytes, benign_small: bytes) -> bytes:
    """Benign contents whose *request body* matches the real upload's.

    The comparison has to be made on the wire, not on the raw text: the public
    endpoint uploads a zip, and repetitive text compresses ~20x while random
    bytes barely compress at all. Padding with random bytes therefore changes the
    body length one-for-one, which is what makes the two probes comparable.
    """
    if not real_bytes:
        return benign_small * 30
    real_body = len(_probe_request_body(real_bytes))
    payload = (BENIGN_PROBE_LINE * (real_body // len(BENIGN_PROBE_LINE) + 2)).encode()
    for _ in range(20):
        delta = real_body - len(_probe_request_body(payload))
        if abs(delta) <= 24:
            break
        if delta > 0:
            payload += secrets.token_bytes(delta)
        else:
            payload = payload[: max(len(BENIGN_PROBE_LINE), len(payload) + delta)]
    return payload


def _probe_request_body(contents: bytes) -> bytes:
    """The exact multipart body the uploader would send for ``contents``."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / DIAGNOSE_FILENAME
        source.write_bytes(contents)
        if _public_archive_enabled():
            archive, archive_name = build_archive(source)
            mime = "application/zip"
        else:
            archive, archive_name, mime = contents, DIAGNOSE_FILENAME, "text/plain"
    body, _ = _encode_multipart(
        {"submitr": "1", "ajax": "1"}, "file", archive_name, archive, mime
    )
    return body


def _explain_probe(results: list[tuple[str, str]]) -> None:
    """Read the size/content matrix back to the operator."""
    answered = [label for label, outcome in results if outcome.startswith("answered")]
    failed = [label for label, outcome in results if not outcome.startswith("answered")]
    if not failed:
        print("\nEvery probe was answered, so the host accepts these requests from "
              "this runner - including a multipart upload of the real size. The "
              "failing upload is then about the real request or a transient host "
              "problem, not a blanket block.")
        return
    first_fail = failed[0]
    print(f"\nThe host stopped answering at: {first_fail}")
    if "small, benign" in first_fail:
        print("Even a tiny benign upload is dropped, so the host objects to the "
              "multipart upload itself (or to this runner), not to the file.")
        return
    small_ok = any("small, benign" in label for label in answered)
    large_benign_ok = any("real size" in label for label in answered)
    real_failed = any("the real file" in label for label in failed)
    if small_ok and not large_benign_ok:
        print("A small upload is answered but a real-size one is not: the host "
              "drops larger request bodies. Try a smaller output file, or "
              "UPLOADKON_TCP_MSS=0 in case the MSS clamp is fragmenting the body.")
        return
    if large_benign_ok and real_failed:
        print("Benign contents of the same size are accepted, so the host is "
              "inspecting and rejecting the file's contents.")
        return
    print("Compare the failing step with the one before it to see what the host "
          "objects to, and use UPLOADKON_PROXY if it is this runner that is "
          "refused.")


def verify_public_link(url: str, timeout: float = 60.0) -> bool:
    """Fetch the returned link once so a broken upload is not reported as success."""
    session = requests.Session()
    try:
        _configure_session(session)
        response = session.get(
            url,
            headers={"User-Agent": _public_upload_headers()["User-Agent"], "Accept": "*/*"},
            timeout=(min(UPLOAD_CONNECT_TIMEOUT, timeout), min(timeout, 60.0)),
            stream=True,
        )
        with response:
            status = response.status_code
            content_type = response.headers.get("Content-Type", "unknown")
            length = response.headers.get("Content-Length", "unknown")
    except requests.exceptions.RequestException as exc:
        message = (f"Could not verify the uploaded link {url}: "
                   f"{type(exc).__name__}: {_one_line(exc)}")
        print(message, file=sys.stderr)
        print(f"::warning::{message}", file=sys.stderr)
        return False
    finally:
        session.close()

    if status == 200:
        message = f"Verified uploaded link: HTTP 200, {content_type}, {length} bytes"
        print(message)
        print(f"::notice::{message}")
        return True
    message = f"Uploaded link returned HTTP {status}: {url}"
    print(message, file=sys.stderr)
    print(f"::warning::{message}", file=sys.stderr)
    return False


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
    # One session for every attempt of this endpoint: the server rotates its
    # cookies on each response, and the next request has to send them back.
    http = requests.Session()
    _configure_session(http)
    try:
        return _upload_attempts(target, path, attempts, timeout, deadline, http)
    finally:
        http.close()


def _upload_attempts(target: str, path: Path, attempts: int, timeout: float,
                     deadline: float, http: requests.Session) -> None:
    """Retry loop for one endpoint, sharing ``http`` (and its cookies) throughout."""
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
            result = _upload_target_once(target, path, min(timeout, remaining), http)
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
            detail = _one_line(exc)
            if not isinstance(exc, UploadError) and isinstance(
                exc, requests.exceptions.ConnectionError
            ):
                # A dropped connection is an operator problem, not a transient one.
                detail = _describe_connection_failure(
                    _upload_host(_public_upload_url() if target == "public"
                                 else _upload_url()),
                    exc,
                )
            message = (
                f"Upload attempt {attempt}/{attempts} via {target} failed: "
                f"{type(exc).__name__}: {detail}"
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
        if target == "public" and isinstance(result, dict):
            link = result.get("direct_url", "")
            if link:
                verify_public_link(link, timeout)
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
        # Never let one endpoint consume the whole budget: the next endpoint must
        # still get a real turn. The last one may use whatever is left.
        remaining_total = deadline - time.monotonic()
        if index + 1 < len(targets):
            target_deadline = time.monotonic() + max(
                MIN_ATTEMPT_SECONDS, remaining_total * (1.0 - RESERVE_FOR_NEXT_ENDPOINT)
            )
            print(
                f"Allowing {max(0.0, target_deadline - time.monotonic()):.0f}s for the "
                f"{target} endpoint; reserving "
                f"{RESERVE_FOR_NEXT_ENDPOINT:.0%} of the remaining budget for "
                f"{targets[index + 1]}."
            )
        else:
            target_deadline = deadline
        try:
            _try_upload_target(target, path, attempts, timeout, min(target_deadline, deadline))
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

    if _diagnose_on_failure():
        try:
            diagnose_upload(path=path)
        except Exception as exc:  # pragma: no cover - diagnostics must not mask the failure
            print(f"Upload host probe could not run: {type(exc).__name__}: {_one_line(exc)}",
                  file=sys.stderr)

    summary = "UploadKon upload failed for every endpoint: " + " | ".join(failures)
    if permanent_only:
        raise PermanentUploadError(summary)
    raise RuntimeError(summary)


def _diagnose_on_failure() -> bool:
    raw = os.getenv("UPLOADKON_DIAGNOSE_ON_FAILURE", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def main() -> int:
    args = sys.argv[1:]
    if "--diagnose-upload" in args:
        # Optionally probe with a specific file: --diagnose-upload [path]
        rest = [a for a in args if a != "--diagnose-upload"]
        return 0 if diagnose_upload(path=Path(rest[0]) if rest else None) else 1
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
