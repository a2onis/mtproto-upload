"""Tests for the MTProto proxy collector."""
import unittest
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from collect_proxies import (
    DEFAULT_LOOKBACK_HOURS,
    RESULTS_DIR,
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


class FilenameTests(unittest.TestCase):
    def test_filename_uses_tehran_local_time(self):
        moment = datetime(2026, 10, 22, 2, 30, 0, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(make_filename(moment), "mtproto-proxies-2026-10-22_06-00-00.txt")
class LookbackTests(unittest.TestCase):
    def test_default_lookback_covers_the_daily_schedule(self):
        # The job runs once every 24 hours, so the window must be a full day.
        self.assertEqual(DEFAULT_LOOKBACK_HOURS, 24)


class ResultsPathTests(unittest.TestCase):
    def test_results_go_to_the_results_folder_with_a_tehran_timestamp(self):
        from collect_proxies import results_path

        moment = datetime(2026, 10, 22, 2, 30, 0, tzinfo=ZoneInfo("UTC"))
        path = results_path(moment)

        self.assertEqual(path.parent, Path(RESULTS_DIR))
        self.assertEqual(path.name, "mtproto-proxies-2026-10-22_06-00-00.txt")
        self.assertEqual(path.suffix, ".txt")

    def test_results_dir_can_be_overridden_by_environment(self):
        import os
        from unittest import mock

        from collect_proxies import results_path

        moment = datetime(2026, 10, 22, 2, 30, 0, tzinfo=ZoneInfo("UTC"))
        with mock.patch.dict(os.environ, {"RESULTS_DIR": "elsewhere/out"}):
            path = results_path(moment)

        self.assertEqual(path, Path("elsewhere/out/mtproto-proxies-2026-10-22_06-00-00.txt"))


class SaveResultsTests(unittest.TestCase):
    def test_creates_the_folder_and_writes_one_config_per_line(self):
        import tempfile

        from collect_proxies import save_results

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "results" / "mtproto-proxies-2026-10-22_06-00-00.txt"
            save_results(["tg://proxy?server=a&port=443&secret=x",
                          "tg://proxy?server=b&port=443&secret=y"], target)

            self.assertTrue(target.exists())
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "tg://proxy?server=a&port=443&secret=x\n"
                "tg://proxy?server=b&port=443&secret=y\n",
            )


class MainTests(unittest.TestCase):
    def _run_main(self, configs, env_extra=None):
        import os
        import tempfile
        from unittest import mock

        import collect_proxies

        env = {"CHANNEL_IDS": "@chan_a,@chan_b"}
        if env_extra:
            env.update(env_extra)

        with tempfile.TemporaryDirectory() as tmp:
            env["RESULTS_DIR"] = str(Path(tmp) / "results")
            fixed = datetime(2026, 10, 24, 20, 30, 0, tzinfo=ZoneInfo("UTC"))
            calls = {}

            async def fake_collect(channel_ids, api_id, api_hash, session, lookback_hours):
                calls["args"] = (channel_ids, api_id, api_hash, session, lookback_hours)
                return configs

            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(collect_proxies, "collect", fake_collect), \
                    mock.patch.object(collect_proxies, "now_tehran", return_value=fixed), \
                    mock.patch("builtins.print"):
                code = collect_proxies.main()
            listing = sorted(p.name for p in Path(env["RESULTS_DIR"]).glob("*.txt"))
            contents = {
                name: (Path(env["RESULTS_DIR"]) / name).read_text(encoding="utf-8")
                for name in listing
            }
        return code, listing, contents, calls

    def test_writes_one_dated_file_into_results(self):
        code, listing, contents, calls = self._run_main(
            ["tg://proxy?server=a&port=443&secret=x"]
        )

        self.assertEqual(code, 0)
        # 2026-10-24 20:30 UTC is 2026-10-25 00:00 in Tehran.
        self.assertEqual(listing, ["mtproto-proxies-2026-10-25_00-00-00.txt"])
        self.assertEqual(
            contents["mtproto-proxies-2026-10-25_00-00-00.txt"],
            "tg://proxy?server=a&port=443&secret=x\n",
        )
        self.assertEqual(calls["args"][0], ["@chan_a", "@chan_b"])
        self.assertEqual(calls["args"][4], DEFAULT_LOOKBACK_HOURS)

    def test_no_results_means_no_file(self):
        code, listing, contents, _ = self._run_main([])

        self.assertEqual(code, 0)
        self.assertEqual(listing, [])

    def test_lookback_hours_is_configurable(self):
        _, _, _, calls = self._run_main(["tg://proxy?server=a&port=443&secret=x"],
                                        {"LOOKBACK_HOURS": "6"})
        self.assertEqual(calls["args"][4], 6)

    def test_missing_channel_ids_is_an_error(self):
        import os
        from unittest import mock

        import collect_proxies

        env = {key: value for key, value in os.environ.items() if key != "CHANNEL_IDS"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                collect_proxies.main()


if __name__ == "__main__":
    unittest.main()
