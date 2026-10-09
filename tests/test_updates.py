"""Opt-in update check (ADR-008): throttled, silent on failure, never trusts
URLs from the network, and points users of a rolled-back release to the
recommended one."""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import updates  # noqa: E402


class _Response(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UpdateCheckTests(unittest.TestCase):
    def test_off_means_never(self):
        self.assertFalse(updates.check_due(False, 0, 10**9))

    def test_at_most_once_a_day(self):
        now = 1_800_000_000
        self.assertTrue(updates.check_due(True, 0, now))
        self.assertFalse(updates.check_due(True, now - 3600, now))
        self.assertTrue(updates.check_due(True, now - 25 * 3600, now))
        self.assertTrue(updates.check_due(True, now + 3600, now))  # clock went back

    def test_recommends_any_different_published_version(self):
        self.assertEqual(updates.recommended_version("v1.2.1", "1.2.0"), "v1.2.1")
        self.assertEqual(updates.recommended_version("v1.2.0", "1.3.0"), "v1.2.0")  # rollback
        self.assertIsNone(updates.recommended_version("v1.2.0", "1.2.0"))
        self.assertIsNone(updates.recommended_version("1.2.0", "1.1.0"))
        self.assertIsNone(updates.recommended_version("v1.2.0-beta", "1.1.0"))
        self.assertIsNone(updates.recommended_version(None, "1.1.0"))

    def test_release_page_is_built_from_a_fixed_prefix(self):
        self.assertEqual(updates.release_url("v1.2.0"),
                         "https://github.com/roko-tech/Anime-Subs-Customizer/releases/tag/v1.2.0")
        self.assertIsNone(updates.release_url("v1.2.0/../../evil"))

    def test_fetch_sends_a_user_agent_and_reads_the_tag(self):
        seen = {}

        def opener(req, timeout):
            seen["ua"] = req.get_header("User-agent")
            seen["url"] = req.full_url
            return _Response({"tag_name": "v1.2.3", "html_url": "https://evil.example"})

        self.assertEqual(updates.fetch_latest_tag(opener=opener), "v1.2.3")
        self.assertTrue(seen["ua"].startswith("AnimeSubsCustomizer/"))
        self.assertEqual(seen["url"], updates.RELEASES_API)

    def test_failures_are_silent(self):
        def rate_limited(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "rate limited", {}, None)

        def offline(req, timeout):
            raise OSError("no network")

        for opener in (rate_limited, offline,
                       lambda r, timeout: _Response(["not", "a", "dict"]),
                       lambda r, timeout: _Response({"tag_name": "nightly"}),
                       lambda r, timeout: _Response({"tag_name": "v1.0.0"}, status=500)):
            self.assertIsNone(updates.fetch_latest_tag(opener=opener))


if __name__ == "__main__":
    unittest.main()
