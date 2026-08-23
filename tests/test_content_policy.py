"""Offline tests for shared download trust boundaries (review ST-C05)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import content_policy  # noqa: E402


class TestAttachmentUrlPolicy(unittest.TestCase):
    def test_allowlisted_exchange_hosts_accepted(self):
        for source, url in (
            ("nse_api",
             "https://nsearchives.nseindia.com/corporate/x.pdf"),
            ("nse_api", "https://www.nseindia.com/corporate/x.pdf"),
            ("bse_api",
             "https://www.bseindia.com/xml-data/corpfiling/AttachLive/g.pdf"),
        ):
            with self.subTest(url=url):
                self.assertIsNone(content_policy.attachment_url_error(source, url))

    def test_non_https_rejected(self):
        self.assertIn("https", content_policy.attachment_url_error(
            "nse_api", "http://nsearchives.nseindia.com/x.pdf") or "")

    def test_unknown_host_rejected(self):
        for url in (
            "https://evil.example.com/x.pdf",
            "https://nsearchives.nseindia.com.attacker.in/x.pdf",
            "https://169.254.169.254/latest/meta-data",
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(
                    content_policy.attachment_url_error("nse_api", url))

    def test_cross_source_host_rejected(self):
        # A BSE host must not be accepted for NSE downloads and vice versa.
        self.assertIsNotNone(content_policy.attachment_url_error(
            "nse_api",
            "https://www.bseindia.com/xml-data/corpfiling/AttachLive/x.pdf"))
        self.assertIsNotNone(content_policy.attachment_url_error(
            "bse_api", "https://nsearchives.nseindia.com/x.pdf"))

    def test_credentialed_or_unparseable_urls_rejected(self):
        self.assertIsNotNone(content_policy.attachment_url_error(
            "bse_api", "https://user:pw@www.bseindia.com/x.pdf"))

    def test_size_caps(self):
        self.assertIsNone(content_policy.download_size_error(1024))
        self.assertIsNotNone(
            content_policy.download_size_error(
                content_policy.MAX_DOWNLOAD_BYTES + 1))
        self.assertIsNone(content_policy.api_body_size_error(1024))
        self.assertIsNotNone(
            content_policy.api_body_size_error(
                content_policy.MAX_API_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
