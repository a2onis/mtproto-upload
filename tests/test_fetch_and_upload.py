import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from fetch_and_upload import extract_proxy_configs, make_filename


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


class FilenameTests(unittest.TestCase):
    def test_filename_uses_tehran_local_time(self):
        moment = datetime(2026, 10, 22, 2, 30, 0, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(make_filename(moment), "mtproto-proxies-2026-10-22_06-00-00.txt")


if __name__ == "__main__":
    unittest.main()
