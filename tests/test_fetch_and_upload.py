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
