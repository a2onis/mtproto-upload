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


class _FakeResponse:
    """Minimal requests.Response stand-in for upload tests."""

    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        import requests

        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self._payload = payload
        self.text = text or ("{}" if payload is not None else "")
        self.cookies = requests.cookies.RequestsCookieJar()

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
