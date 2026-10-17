import unittest
from unittest.mock import patch

from api.preview import preview_listings
from depto_monitor.dedupe import deduplicate
from depto_monitor.models import Listing, Portal, SourceURL
from depto_monitor.whatsapp import format_summary


def listing(source: Portal, url: str) -> Listing:
    return Listing(source=source, source_url=url, url=url, title="2 amb Palermo", address="Costa Rica 5000", price="600000", currency="ARS", rooms="2", covered_m2="45")


class MonitorTests(unittest.TestCase):
    def test_deduplicates_cross_portal_listing(self):
        first = listing(Portal.ZONAPROP, "https://www.zonaprop.com.ar/a")
        second = listing(Portal.ARGENPROP, "https://www.argenprop.com/b")
        self.assertEqual(len(deduplicate([first, second])), 1)

    def test_rejects_url_from_wrong_portal(self):
        with self.assertRaises(ValueError):
            SourceURL(portal=Portal.ZONAPROP, url="https://example.com/search")

    def test_summary_is_empty_without_changes(self):
        self.assertEqual(format_summary("Palermo", {}), "")

    def test_preview_rejects_a_url_outside_the_selected_portal(self):
        with self.assertRaises(ValueError):
            preview_listings({"portal": "zonaprop", "url": "https://example.com/results"})

    @patch("api.preview.fetch_source")
    def test_preview_returns_serializable_listings(self, fetch_source):
        source_url = "https://www.zonaprop.com.ar/alquileres.html"
        fetch_source.return_value = [listing(Portal.ZONAPROP, source_url)]

        result = preview_listings({"portal": "zonaprop", "url": source_url})

        self.assertEqual(result["found"], 1)
        self.assertEqual(result["listings"][0]["title"], "2 amb Palermo")


if __name__ == "__main__":
    unittest.main()
