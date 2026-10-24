import json
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fetch_and_upload import (
    DEFAULT_LOOKBACK_HOURS,
    extract_proxy_configs,
    make_filename,
    normalize_channel_username,
    parse_web_preview_page,
)

try:  # Upload tests need requests; skip them instead of failing on a bare interpreter.
    import requests as _requests  # noqa: F401

    HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    HAS_REQUESTS = False


class ProxyExtractionTests(unittest.TestCase):
    def test_extracts_and_normalizes_mtproto_links(self):
        text = (
            "Proxy: https://t.me/proxy?server=proxy.example&port=443&secret=abcdef. "
            "Also tg://proxy?server=proxy.example&port=443&secret=abcdef"
        )
        self.assertEqual(
            extract_proxy_configs(text),
            ["tg://proxy?server=proxy.example&port=443&secret=abcdef"],
        )

    def test_extracts_html_escaped_links(self):
        html_snippet = (
            '<a href="https://t.me/proxy?server=proxy.example&amp;port=443&amp;secret=abcdef">'
            "Connect</a>"
        )
        self.assertEqual(
            extract_proxy_configs(html_snippet),
            ["tg://proxy?server=proxy.example&port=443&secret=abcdef"],
        )

    def test_ignores_incomplete_or_invalid_links(self):
        text = (
            "tg://proxy?server=host&port=0&secret=abc "
            "tg://proxy?server=host&port=443 "
            "https://example.org/not-a-proxy?server=host&port=443&secret=abc"
        )
        self.assertEqual(extract_proxy_configs(text), [])

    def test_keeps_distinct_configs(self):
        text = "tg://proxy?server=a&port=80&secret=x tg://proxy?server=b&port=443&secret=y"
        self.assertEqual(len(extract_proxy_configs(text)), 2)


class ChannelNormalizationTests(unittest.TestCase):
    def test_normalizes_public_usernames_and_urls(self):
        self.assertEqual(normalize_channel_username("@public_chan"), "public_chan")
        self.assertEqual(normalize_channel_username("public_chan"), "public_chan")
        self.assertEqual(normalize_channel_username("https://t.me/s/public_chan"), "public_chan")
        self.assertEqual(normalize_channel_username("https://t.me/public_chan"), "public_chan")
        self.assertIsNone(normalize_channel_username("-100123456789"))


class WebPreviewParsingTests(unittest.TestCase):
    def test_parses_newest_first_until_cutoff(self):
        page = """
        <div class="tgme_widget_message" data-post="chan/10">
          <time datetime="2026-10-22T01:00:00+00:00"></time>
          <a href="https://t.me/proxy?server=old.example&amp;port=443&amp;secret=1111">Old</a>
        </div>
        <div class="tgme_widget_message" data-post="chan/11">
          <time datetime="2026-10-22T15:00:00+00:00"></time>
          <a href="https://t.me/proxy?server=new.example&amp;port=443&amp;secret=2222">New</a>
        </div>
        """
        cutoff = datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)
        configs, oldest_id, reached_cutoff = parse_web_preview_page(page, cutoff)
        self.assertEqual(
            configs,
            ["tg://proxy?server=new.example&port=443&secret=2222"],
        )
        self.assertEqual(oldest_id, 10)
        self.assertTrue(reached_cutoff)


class LookbackTests(unittest.TestCase):
    def test_default_lookback_is_twelve_hours(self):
        self.assertEqual(DEFAULT_LOOKBACK_HOURS, 12)


class FilenameTests(unittest.TestCase):
    def test_filename_uses_tehran_local_time(self):
        moment = datetime(2026, 10, 22, 2, 30, 0, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(make_filename(moment), "mtproto-proxies-2026-10-22_06-00-00.txt")


if __name__ == "__main__":
    unittest.main()


class _HeaderBag:
    """Mimics urllib3's HTTPHeaderDict, which can repeat a header name."""

    def __init__(self, pairs):
        self._pairs = list(pairs)

    def getlist(self, name):
        lowered = name.lower()
        return [value for key, value in self._pairs if key.lower() == lowered]


class _FakeResponse:
    """Minimal requests.Response stand-in for upload tests."""

    def __init__(self, status_code: int = 200, payload=None, text: str = "",
                 set_cookies=()):
        import requests

        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self._payload = payload
        self.text = text or ("{}" if payload is not None else "")
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers: dict[str, str] = {}
        self.raw = None
        if set_cookies:
            self.raw = type("Raw", (), {})()
            self.raw.headers = _HeaderBag(
                [("Set-Cookie", value) for value in set_cookies]
            )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        if self._payload is None:
            raise ValueError("no json in response")
        return self._payload


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class UploadRetryTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mtproto-proxies-test.txt"
        self.path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")

        env = {
            "UPLOADKON_COOKIE": "aplod_kn_session=abc; XSRF-TOKEN=def",
            "UPLOADKON_XSRF_TOKEN": "def",
            "UPLOADKON_MODE": "drive",
        }
        env_patcher = mock.patch.dict(os.environ, env)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _install_session(self, get_response, post_side_effect):
        from unittest import mock

        session = mock.MagicMock()
        session.get.return_value = get_response
        session.post.side_effect = post_side_effect
        patcher = mock.patch("fetch_and_upload.requests.Session", return_value=session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def _patch_sleep(self):
        from unittest import mock

        patcher = mock.patch("fetch_and_upload.time.sleep")
        sleep = patcher.start()
        self.addCleanup(patcher.stop)
        return sleep

    def test_retries_after_read_timeout_then_succeeds(self):
        import requests
        from fetch_and_upload import upload_file

        session = self._install_session(
            _FakeResponse(200, {"ok": True}),
            [
                requests.exceptions.ReadTimeout("read timed out"),
                _FakeResponse(200, {"data": {"url": "https://my.uploadkon.ir/d/abc"}}),
            ],
        )
        sleep = self._patch_sleep()

        upload_file(self.path)

        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once()

    def test_gives_up_after_configured_attempts(self):
        import os
        import requests
        from fetch_and_upload import upload_file

        os.environ["UPLOADKON_MAX_ATTEMPTS"] = "3"
        session = self._install_session(
            _FakeResponse(200, {"ok": True}),
            requests.exceptions.ReadTimeout("read timed out"),
        )
        sleep = self._patch_sleep()

        with self.assertRaises(RuntimeError) as ctx:
            upload_file(self.path)

        self.assertIn("after 3 attempt(s)", str(ctx.exception))
        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_retries_server_errors(self):
        import requests
        from fetch_and_upload import upload_file

        session = self._install_session(
            _FakeResponse(200, {"ok": True}),
            [
                _FakeResponse(502, text="bad gateway"),
                _FakeResponse(200, {"ok": True}),
            ],
        )
        sleep = self._patch_sleep()

        upload_file(self.path)

        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once()

    def test_does_not_retry_permanent_client_errors(self):
        from fetch_and_upload import PermanentUploadError, upload_file

        session = self._install_session(
            _FakeResponse(200, {"ok": True}),
            [_FakeResponse(400, text="invalid upload request")],
        )
        sleep = self._patch_sleep()

        with self.assertRaises(PermanentUploadError):
            upload_file(self.path)

        self.assertEqual(session.post.call_count, 1)
        sleep.assert_not_called()

    def test_retries_when_session_cookie_is_rejected(self):
        import os
        import requests
        from fetch_and_upload import upload_file

        os.environ["UPLOADKON_MAX_ATTEMPTS"] = "2"
        session = self._install_session(
            _FakeResponse(419, text="page expired"),
            _FakeResponse(200, {"ok": True}),
        )
        sleep = self._patch_sleep()

        with self.assertRaises(RuntimeError) as ctx:
            upload_file(self.path)

        self.assertIn("after 2 attempt(s)", str(ctx.exception))
        self.assertEqual(session.get.call_count, 2)
        session.post.assert_not_called()
        sleep.assert_called_once()


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class UploadLinkTests(unittest.TestCase):
    def test_finds_nested_download_link(self):
        from fetch_and_upload import _find_upload_link

        payload = {
            "data": {
                "file": {"name": "mtproto.txt", "downloadUrl": "https://my.uploadkon.ir/d/xyz"}
            }
        }
        self.assertEqual(
            _find_upload_link(payload), "https://my.uploadkon.ir/d/xyz"
        )

    def test_returns_none_without_link(self):
        from fetch_and_upload import _find_upload_link

        self.assertIsNone(_find_upload_link({"ok": True, "data": {"id": 5}}))


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class SessionRefreshTests(unittest.TestCase):
    """A slow session endpoint must not block the upload POST."""

    def test_network_error_during_refresh_still_uploads(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        import requests as requests_module

        from fetch_and_upload import upload_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")

            session = mock.MagicMock()
            session.get.side_effect = requests_module.exceptions.ReadTimeout("slow")
            session.post.return_value = _FakeResponse(200, {"ok": True})

            with mock.patch.dict(
                os.environ,
                {
                    "UPLOADKON_COOKIE": "aplod_kn_session=abc",
                    "UPLOADKON_XSRF_TOKEN": "def",
                    "UPLOADKON_MODE": "drive",
                },
            ), mock.patch("fetch_and_upload.requests.Session", return_value=session):
                upload_file(path)

            session.post.assert_called_once()
            headers = session.post.call_args.kwargs["headers"]
            self.assertEqual(headers["Cookie"], "aplod_kn_session=abc")
            self.assertEqual(headers["X-XSRF-TOKEN"], "def")


ROTATED_XSRF_COOKIE = (
    "XSRF-TOKEN=eyJpdiI6InFnaXo3SWg0ajk4dHJVZ0NGMmN4Q3c9PSJ9%3D; "
    "expires=Sat, 24 Oct 2026 20:14:53 GMT; Max-Age=7200; path=/; secure; samesite=lax"
)
ROTATED_SESSION_COOKIE = (
    "aplod_kn_session=eyJpdiI6IjNQa3A4MjJxRzNlWnJxOVEvNDVCanc9PSJ9%3D; "
    "expires=Sat, 24 Oct 2026 20:14:53 GMT; Max-Age=7200; path=/; secure; httponly; "
    "samesite=lax"
)


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class ResponseCookieCarryoverTests(unittest.TestCase):
    """Cookies from the latest response headers must be sent on the next request."""

    def test_stores_every_set_cookie_header_including_repeated_names(self):
        from unittest import mock

        import requests

        from fetch_and_upload import _store_response_cookies

        session = requests.Session()
        response = _FakeResponse(
            200,
            set_cookies=[ROTATED_XSRF_COOKIE, ROTATED_SESSION_COOKIE, "locale=en"],
        )
        stored = _store_response_cookies(
            session, response, "https://my.uploadkon.ir/api/v1/drive/file-entries"
        )

        self.assertEqual(
            sorted(stored), ["XSRF-TOKEN", "aplod_kn_session", "locale"]
        )
        # The value must be the raw cookie value, without the attributes.
        self.assertTrue(stored["aplod_kn_session"].startswith("eyJpdiI6IjNQa3A4"))
        self.assertNotIn("Max-Age", stored["aplod_kn_session"])
        self.assertEqual(session.cookies.get("XSRF-TOKEN", domain="my.uploadkon.ir"),
                         "eyJpdiI6InFnaXo3SWg0ajk4dHJVZ0NGMmN4Q3c9PSJ9%3D")

    def test_cookies_from_an_error_response_are_kept_too(self):
        from unittest import mock

        import requests

        from fetch_and_upload import _store_response_cookies

        session = requests.Session()
        response = _FakeResponse(419, text="page expired", set_cookies=[ROTATED_XSRF_COOKIE])
        stored = _store_response_cookies(session, response, "https://my.uploadkon.ir/x")

        self.assertIn("XSRF-TOKEN", stored)
        self.assertEqual(session.cookies.get("XSRF-TOKEN", domain="my.uploadkon.ir"),
                         "eyJpdiI6InFnaXo3SWg0ajk4dHJVZ0NGMmN4Q3c9PSJ9%3D")

    def test_drive_upload_sends_the_rotated_cookie_and_token(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        from fetch_and_upload import upload_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")

            refresh = _FakeResponse(
                200, {"data": []}, set_cookies=[ROTATED_XSRF_COOKIE, ROTATED_SESSION_COOKIE]
            )
            session = mock.MagicMock()
            session.get.return_value = refresh
            session.post.return_value = _FakeResponse(
                200, {"data": {"url": "https://my.uploadkon.ir/d/abc"}}
            )

            env = {
                "UPLOADKON_MODE": "drive",
                "UPLOADKON_COOKIE": "aplod_kn_session=stale-session; XSRF-TOKEN=stale-token",
                "UPLOADKON_XSRF_TOKEN": "stale-token",
            }
            with mock.patch.dict(os.environ, env), \
                    mock.patch("fetch_and_upload.requests.Session", return_value=session):
                upload_file(path)

            headers = session.post.call_args.kwargs["headers"]
            sent = dict(
                part.split("=", 1) for part in headers["Cookie"].split("; ")
            )
            # The newest values from the response replace the configured ones.
            self.assertEqual(
                sent["aplod_kn_session"], "eyJpdiI6IjNQa3A4MjJxRzNlWnJxOVEvNDVCanc9PSJ9%3D"
            )
            self.assertEqual(
                sent["XSRF-TOKEN"], "eyJpdiI6InFnaXo3SWg0ajk4dHJVZ0NGMmN4Q3c9PSJ9%3D"
            )
            # Laravel compares the URL-decoded header against the XSRF-TOKEN cookie.
            self.assertEqual(
                headers["X-XSRF-TOKEN"], 'eyJpdiI6InFnaXo3SWg0ajk4dHJVZ0NGMmN4Q3c9PSJ9='
            )
            self.assertNotIn("stale", headers["Cookie"])

    def test_rotated_cookie_from_a_rejected_upload_is_sent_by_the_next_request(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        import requests

        from fetch_and_upload import upload_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")

            # A real jar on the mock so stored cookies can be inspected.
            jar = requests.cookies.RequestsCookieJar()
            session = mock.MagicMock()
            session.cookies = jar
            session.get.return_value = _FakeResponse(200, {"data": []})
            session.post.side_effect = [
                _FakeResponse(419, text="page expired", set_cookies=[ROTATED_XSRF_COOKIE]),
                _FakeResponse(200, {"data": {"url": "https://my.uploadkon.ir/d/abc"}}),
            ]
            env = {
                "UPLOADKON_MODE": "drive",
                "UPLOADKON_COOKIE": "aplod_kn_session=s1; XSRF-TOKEN=old",
                "UPLOADKON_XSRF_TOKEN": "old",
            }
            with mock.patch.dict(os.environ, env), \
                    mock.patch("fetch_and_upload.requests.Session", return_value=session), \
                    mock.patch("fetch_and_upload.time.sleep"):
                upload_file(path)

            self.assertEqual(session.post.call_count, 2)
            # The 419 reply rotated the token; the retry must send the new value,
            # not the stale one from the secret.
            retry_cookie = session.get.call_args_list[1].kwargs["headers"]["Cookie"]
            self.assertIn(
                "XSRF-TOKEN=eyJpdiI6InFnaXo3SWg0ajk4dHJVZ0NGMmN4Q3c9PSJ9%3D", retry_cookie
            )
            self.assertNotIn("XSRF-TOKEN=old", retry_cookie)
            self.assertEqual(
                session.post.call_args_list[1].kwargs["headers"]["X-XSRF-TOKEN"],
                "eyJpdiI6InFnaXo3SWg0ajk4dHJVZ0NGMmN4Q3c9PSJ9=",
            )


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class DroppedConnectionTests(unittest.TestCase):
    """uploadkon.ir/my.uploadkon.ir answer some requests by closing the socket."""

    def test_requests_keep_the_connection_alive_by_default(self):
        import os
        from unittest import mock

        from fetch_and_upload import _public_upload_headers, _upload_base_headers

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UPLOADKON_CONNECTION_CLOSE", None)
            self.assertNotIn("Connection", _public_upload_headers())
            self.assertNotIn("Connection", _upload_base_headers())

        with mock.patch.dict(os.environ, {"UPLOADKON_CONNECTION_CLOSE": "1"}):
            self.assertEqual(_public_upload_headers()["Connection"], "close")

    def test_session_retries_a_connection_the_host_drops(self):
        import os
        from unittest import mock

        import requests

        from fetch_and_upload import _configure_session

        with mock.patch.dict(os.environ, {"UPLOADKON_TRANSPORT_RETRIES": "2"}):
            session = requests.Session()
            _configure_session(session)

        adapter = session.get_adapter("https://my.uploadkon.ir/api/v1/file-entries")
        retry = adapter.max_retries
        self.assertEqual(retry.connect, 2)
        self.assertEqual(retry.total, 2)
        # Read timeouts belong to the outer attempt loop, not the transport.
        self.assertEqual(retry.read, 0)
        self.assertIn("POST", retry.allowed_methods)

    def test_transport_retries_can_be_disabled(self):
        import os
        from unittest import mock

        import requests

        from fetch_and_upload import _configure_session

        with mock.patch.dict(os.environ, {"UPLOADKON_TRANSPORT_RETRIES": "0"}):
            session = requests.Session()
            _configure_session(session)

        adapter = session.get_adapter("https://uploadkon.ir/")
        # An int max_retries means "no transport retry at all".
        self.assertEqual(adapter.max_retries.total, 0)
        self.assertIsNone(adapter.max_retries.connect)

    def test_mss_clamp_can_be_disabled(self):
        import os
        from unittest import mock

        from urllib3.connection import HTTPConnection

        from fetch_and_upload import _configure_tcp_mss

        import socket

        clamp = (socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1000)
        original = list(HTTPConnection.default_socket_options)
        self.addCleanup(
            setattr, HTTPConnection, "default_socket_options", original
        )

        with mock.patch.dict(os.environ, {"UPLOADKON_TCP_MSS": "0"}):
            _configure_tcp_mss()
        self.assertNotIn(clamp, HTTPConnection.default_socket_options)

        with mock.patch.dict(os.environ, {"UPLOADKON_TCP_MSS": "1000"}):
            _configure_tcp_mss()
        self.assertIn(clamp, HTTPConnection.default_socket_options)

    def test_warm_up_is_skipped_when_cookies_are_already_held(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        import requests

        from fetch_and_upload import upload_file

        body = (
            '[{"message_content": "<textarea id=\\"file1\\">'
            "https://uploadkon.ir/uploads/p.txt</textarea></textarea>\"}]"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")

            jar = requests.cookies.RequestsCookieJar()
            jar.set("anti_bot", "1")
            session = mock.MagicMock()
            session.cookies = jar
            session.post.return_value = _FakeResponse(200, text=body)

            with mock.patch.dict(os.environ, {"UPLOADKON_MODE": "public"}), \
                    mock.patch("fetch_and_upload.requests.Session", return_value=session), \
                    mock.patch("builtins.print"):
                upload_file(path)

        # No warm-up page load: the only GET is verify_public_link on the result.
        warm_up_calls = [
            call for call in session.get.call_args_list
            if call.args and call.args[0].rstrip("/") == "https://uploadkon.ir"
        ]
        self.assertEqual(warm_up_calls, [])
        session.post.assert_called_once()


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class DroppedSocketEndToEndTests(unittest.TestCase):
    """A real server that RSTs the first request must be survived by the retry."""

    def test_upload_succeeds_after_the_first_upload_post_is_dropped(self):
        import os
        import socket
        import tempfile
        import threading
        from pathlib import Path
        from unittest import mock

        from fetch_and_upload import upload_file

        state = {"posts": 0}

        class Handler:
            def __init__(self, conn):
                self.conn = conn

            def serve(self):
                try:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = self.conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    first = data.startswith(b"POST")
                    if first:
                        state["posts"] += 1
                        first = state["posts"] == 1
                    if first:
                        # Reproduce "('Connection aborted.', RemoteDisconnected(...))":
                        # close without sending a single byte of HTTP.
                        self.conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                             __import__("struct").pack("ii", 1, 0))
                        self.conn.close()
                        return
                    body = (
                        b'[{"message_content": "<textarea id=\\"file1\\">'
                        b"https://uploadkon.ir/uploads/ok.txt</textarea>\"}]"
                    )
                    self.conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                    )
                finally:
                    try:
                        self.conn.close()
                    except OSError:
                        pass

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]

        def accept_loop():
            for _ in range(16):
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                threading.Thread(target=Handler(conn).serve, daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()
        self.addCleanup(listener.close)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")
            env = {
                "UPLOADKON_MODE": "public",
                "UPLOADKON_PUBLIC_URL": f"http://127.0.0.1:{port}/",
                "UPLOADKON_PUBLIC_ARCHIVE": "1",
                "UPLOADKON_TRANSPORT_RETRIES": "2",
                "UPLOADKON_CONNECTION_CLOSE": "1",
                "UPLOADKON_MAX_ATTEMPTS": "1",
            }
            with mock.patch.dict(os.environ, env), mock.patch("builtins.print"):
                upload_file(path)

        # Only one outer attempt was allowed, so the recovery had to happen
        # below requests: the upload POST was sent twice on two sockets.
        self.assertEqual(state["posts"], 2)

    def test_repeatedly_dropped_connection_reports_the_runner_ip(self):
        import os
        import socket
        import tempfile
        import threading
        from pathlib import Path
        from unittest import mock

        from fetch_and_upload import upload_file

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]

        def accept_loop():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                __import__("struct").pack("ii", 1, 0))
                conn.close()

        threading.Thread(target=accept_loop, daemon=True).start()
        self.addCleanup(listener.close)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")
            env = {
                "UPLOADKON_MODE": "public",
                "UPLOADKON_PUBLIC_URL": f"http://127.0.0.1:{port}/",
                "UPLOADKON_TRANSPORT_RETRIES": "1",
                "UPLOADKON_MAX_ATTEMPTS": "1",
            }
            with mock.patch.dict(os.environ, env), mock.patch("builtins.print"), \
                    mock.patch("fetch_and_upload._egress_ip", return_value="203.0.113.7"):
                with self.assertRaises(RuntimeError) as ctx:
                    upload_file(path)

        message = str(ctx.exception)
        self.assertIn("203.0.113.7", message)
        self.assertIn("UPLOADKON_PROXY", message)


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class DiagnoseUploadTests(unittest.TestCase):
    """The probe must name the request the host drops, not guess at the cause."""

    def _server(self, drop_post):
        import http.server
        import socket
        import socketserver
        import struct
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self):
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _drain(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)

            def do_GET(self):
                self._drain()
                self._send()

            def do_POST(self):
                self._drain()
                if drop_post:
                    self.close_connection = True
                    self.connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                    self.connection.close()
                    return
                self._send()

            def log_message(self, *args):
                pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    def test_probe_reports_the_step_where_the_host_stops_answering(self):
        import os
        from unittest import mock

        from fetch_and_upload import diagnose_upload

        url = self._server(drop_post=True)
        with mock.patch.dict(os.environ, {"UPLOADKON_PUBLIC_URL": url,
                                          "UPLOADKON_URL": url + "api/v1/file-entries"}), \
                mock.patch("fetch_and_upload._egress_ip", return_value="203.0.113.9"), \
                mock.patch("builtins.print") as printed:
            self.assertFalse(diagnose_upload(timeout=10))

        output = "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        # GETs were answered; the first unanswered request is a POST.
        self.assertIn("answered HTTP 200", output)
        self.assertIn("FAIL", output)
        self.assertIn("POST", output)
        # The cause must not be asserted - only the observable behaviour.
        self.assertNotIn("host refusing the runner", output)

    def test_probe_reports_success_when_everything_is_answered(self):
        import os
        from unittest import mock

        from fetch_and_upload import diagnose_upload

        url = self._server(drop_post=False)
        with mock.patch.dict(os.environ, {"UPLOADKON_PUBLIC_URL": url,
                                          "UPLOADKON_URL": url + "api/v1/file-entries"}), \
                mock.patch("fetch_and_upload._egress_ip", return_value="203.0.113.9"), \
                mock.patch("builtins.print"):
            self.assertTrue(diagnose_upload(timeout=10))

    def test_connection_failure_message_does_not_claim_the_host_is_blocking(self):
        from unittest import mock

        import requests

        from fetch_and_upload import _describe_connection_failure

        exc = requests.exceptions.ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed connection "
            "without response'))"
        )
        with mock.patch("fetch_and_upload._egress_ip", return_value="203.0.113.9"):
            message = _describe_connection_failure("uploadkon.ir", exc)

        self.assertIn("203.0.113.9", message)
        self.assertIn("--diagnose-upload", message)
        self.assertNotIn("not a bug in the request", message)


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class EndpointBudgetTests(unittest.TestCase):
    """A host that ignores every request must not starve the fallback endpoint."""

    def test_fallback_endpoint_still_gets_budget_after_the_first_one_burns_it(self):
        import os
        import tempfile
        import time
        from pathlib import Path
        from unittest import mock

        from fetch_and_upload import upload_file

        allowances: list[float] = []

        def fake_try(target, path, attempts, timeout, deadline):
            allowances.append(deadline - time.monotonic())
            if target == "public":
                # Simulate the public endpoint burning every second it was given.
                time.sleep(0.2)
                raise RuntimeError("public upload failed after 4 attempt(s)")
            raise RuntimeError("drive upload failed")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")
            env = {
                "UPLOADKON_MODE": "auto",
                "UPLOADKON_COOKIE": "aplod_kn_session=abc",
                "UPLOADKON_TOTAL_TIMEOUT": "600",
                "UPLOADKON_DIAGNOSE_ON_FAILURE": "0",
            }
            with mock.patch.dict(os.environ, env), \
                    mock.patch("fetch_and_upload._try_upload_target", side_effect=fake_try), \
                    mock.patch("builtins.print"):
                with self.assertRaises(RuntimeError):
                    upload_file(path)

        self.assertEqual(len(allowances), 2)
        # The first endpoint is capped so it cannot eat the whole budget, and the
        # last one may use everything that is left. Before the fix the second
        # endpoint received a negative allowance ("-21s of the upload budget").
        self.assertGreater(allowances[0], 0)
        self.assertLess(allowances[0], 600)
        self.assertGreater(allowances[1], 100)

    def test_probe_runs_when_every_endpoint_fails(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        from fetch_and_upload import upload_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")
            env = {"UPLOADKON_MODE": "public", "UPLOADKON_MAX_ATTEMPTS": "1"}
            with mock.patch.dict(os.environ, env), \
                    mock.patch("fetch_and_upload._try_upload_target",
                               side_effect=RuntimeError("nope")), \
                    mock.patch("fetch_and_upload.diagnose_upload", return_value=True) as probe, \
                    mock.patch("builtins.print"):
                with self.assertRaises(RuntimeError):
                    upload_file(path)

        probe.assert_called_once()


class PublicResponseParsingTests(unittest.TestCase):
    def test_extracts_direct_and_delete_links(self):
        from fetch_and_upload import parse_public_upload_response

        fragment = (
            "<div class='alert alert-success'>"
            "<textarea id='file1' rows='3'>https://uploadkon.ir/uploads/abc.txt</textarea>"
            "<textarea id='delCode'>del123</textarea></div>"
        )
        result = parse_public_upload_response(json.dumps([{"message_content": fragment}]))
        self.assertEqual(result["direct_url"], "https://uploadkon.ir/uploads/abc.txt")
        self.assertEqual(result["delete_code"], "del123")

    def test_accepts_i_field_and_html_entities(self):
        from fetch_and_upload import parse_public_upload_response

        result = parse_public_upload_response(
            '{"i": "<textarea id=\\"file1\\">https://uploadkon.ir/uploads/x.txt?a=1&amp;b=2</textarea>"}'
        )
        self.assertEqual(result["direct_url"], "https://uploadkon.ir/uploads/x.txt?a=1&b=2")

    def test_rejection_without_link_is_permanent(self):
        from fetch_and_upload import PermanentUploadError, parse_public_upload_response

        with self.assertRaises(PermanentUploadError):
            parse_public_upload_response('[{"status": false, "message": "file too large"}]')

    def test_unsupported_extension_rejection_is_reported(self):
        from fetch_and_upload import PermanentUploadError, parse_public_upload_response

        payload = (
            '[{"t": "index_err", "i": "پسوند \\"txt\\" پشتیبانی نمی‌شود!<br> '
            '<a href=\\"ucp.php?go=register\\">عضویت</a>", '
            '"message_content": "<div>rejected</div>"}]'
        )
        with self.assertRaises(PermanentUploadError) as ctx:
            parse_public_upload_response(payload)
        message = str(ctx.exception)
        self.assertIn("txt", message)
        self.assertIn("پشتیبانی نمی‌شود", message)

    def test_non_json_reply_is_retryable(self):
        from fetch_and_upload import RetryableUploadError, parse_public_upload_response

        with self.assertRaises(RetryableUploadError):
            parse_public_upload_response("<html>502 Bad Gateway</html>")


class ArchiveTests(unittest.TestCase):
    def test_archive_contains_the_proxy_list(self):
        import tempfile
        import zipfile
        from pathlib import Path

        from fetch_and_upload import build_archive

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-2026-10-23_12-00-00.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")
            data, archive_name = build_archive(path)

        self.assertEqual(archive_name, path.name + ".zip")
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), [path.name])
            self.assertIn("tg://proxy", archive.read(path.name).decode())


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class UploadModeTests(unittest.TestCase):
    def test_auto_prefers_public_then_drive(self):
        from fetch_and_upload import _upload_targets

        self.assertEqual(_upload_targets("auto", True), ["public", "drive"])
        self.assertEqual(_upload_targets("auto", False), ["public"])
        self.assertEqual(_upload_targets("public", True), ["public"])
        self.assertEqual(_upload_targets("drive", True), ["drive"])

    def test_unknown_mode_is_rejected(self):
        from fetch_and_upload import PermanentUploadError, _upload_targets

        with self.assertRaises(PermanentUploadError):
            _upload_targets("ftp", True)

    def test_public_success_reports_direct_link(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        from fetch_and_upload import upload_file

        body = '[{"message_content": "<textarea id=\\"file1\\">https://uploadkon.ir/uploads/p.txt</textarea><textarea id=\\"delCode\\">del789</textarea>"}]'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")

            session = mock.MagicMock()
            session.post.return_value = _FakeResponse(200, text=body)
            with mock.patch.dict(os.environ, {"UPLOADKON_MODE": "public"}), \
                    mock.patch("fetch_and_upload.requests.Session", return_value=session), \
                    mock.patch("builtins.print") as printed:
                upload_file(path)

            self.assertEqual(session.post.call_count, 1)
            output = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
            self.assertIn("https://uploadkon.ir/uploads/p.txt", output)
            self.assertIn("del789", output)

    def test_falls_back_to_drive_when_public_fails(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        from fetch_and_upload import upload_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")

            session = mock.MagicMock()

            def post(url, **kwargs):
                if url == "https://uploadkon.ir/":
                    return _FakeResponse(503, text="service unavailable")
                return _FakeResponse(200, {"data": {"url": "https://my.uploadkon.ir/d/ok"}})

            session.post.side_effect = post
            session.get.return_value = _FakeResponse(200, {"data": []})
            env = {
                "UPLOADKON_MODE": "auto",
                "UPLOADKON_COOKIE": "aplod_kn_session=abc",
                "UPLOADKON_XSRF_TOKEN": "def",
                "UPLOADKON_MAX_ATTEMPTS": "1",
            }
            with mock.patch.dict(os.environ, env), \
                    mock.patch("fetch_and_upload.requests.Session", return_value=session), \
                    mock.patch("builtins.print"):
                upload_file(path)

            urls = [c.args[0] if c.args else c.kwargs.get("url") for c in session.post.call_args_list]
            self.assertTrue(any(u == "https://uploadkon.ir/" for u in urls), urls)
            self.assertTrue(any(u.startswith("https://my.uploadkon.ir") for u in urls), urls)


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class UploadBudgetTests(unittest.TestCase):
    def test_stops_retrying_when_budget_is_exhausted(self):
        import os
        import tempfile
        import time
        from pathlib import Path
        from unittest import mock

        import requests as requests_module

        from fetch_and_upload import upload_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mtproto-proxies-test.txt"
            path.write_text("tg://proxy?server=a&port=443&secret=b\n", encoding="utf-8")

            session = mock.MagicMock()
            session.post.side_effect = requests_module.exceptions.ReadTimeout("slow")
            env = {
                "UPLOADKON_MODE": "public",
                "UPLOADKON_MAX_ATTEMPTS": "50",
                "UPLOADKON_TIMEOUT": "1",
                "UPLOADKON_TOTAL_TIMEOUT": "15",
            }
            started = time.monotonic()
            with mock.patch.dict(os.environ, env), \
                    mock.patch("fetch_and_upload.requests.Session", return_value=session), \
                    mock.patch("builtins.print"):
                with self.assertRaises(RuntimeError):
                    upload_file(path)
            elapsed = time.monotonic() - started

            # The 15s budget must end the loop long before 50 attempts.
            self.assertLess(elapsed, 15)
            self.assertLess(session.post.call_count, 50)


@unittest.skipUnless(HAS_REQUESTS, "requests is not installed")
class LinkVerificationTests(unittest.TestCase):
    def _session(self, response):
        from unittest import mock

        session = mock.MagicMock()
        session.get.return_value = response
        return session

    def test_ok_link_is_verified(self):
        from unittest import mock

        from fetch_and_upload import verify_public_link

        response = _FakeResponse(200, text="zipbytes")
        response.headers = {"Content-Type": "application/zip", "Content-Length": "9"}
        with mock.patch("fetch_and_upload.requests.Session",
                        return_value=self._session(response)):
            self.assertTrue(verify_public_link("https://uploadkon.ir/uploads/x.zip"))

    def test_broken_link_warns_without_raising(self):
        import requests as requests_module
        from unittest import mock

        from fetch_and_upload import verify_public_link

        session = mock.MagicMock()
        session.get.side_effect = requests_module.exceptions.ReadTimeout("slow")
        with mock.patch("fetch_and_upload.requests.Session", return_value=session), \
                mock.patch("builtins.print"):
            self.assertFalse(verify_public_link("https://uploadkon.ir/uploads/x.zip"))
