import unittest
from unittest.mock import patch

from api.preview import preview_listings
from depto_monitor.dedupe import deduplicate
from depto_monitor.models import Listing, Portal, SearchFilters, SourceURL
from depto_monitor.searches import LiveSearchRequest, build_source_urls, matches_filters
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

    def test_preview_requires_a_location(self):
        with self.assertRaises(ValueError):
            preview_listings({"portals": ["zonaprop"], "filters": {"locations": []}})

    @patch("depto_monitor.searches.fetch_source")
    def test_preview_returns_serializable_listings(self, fetch_source):
        source_url = "https://www.zonaprop.com.ar/departamentos-alquiler-palermo.html"
        fetch_source.return_value = [listing(Portal.ZONAPROP, source_url)]

        result = preview_listings({"portals": ["zonaprop"], "filters": {"locations": ["Palermo"]}})

        self.assertEqual(result["found"], 1)
        self.assertEqual(result["listings"][0]["title"], "2 amb Palermo")

    def test_builds_sources_from_portals_and_location(self):
        request = LiveSearchRequest.model_validate({"portals": ["argenprop", "zonaprop"], "filters": {"locations": ["CABA"]}})
        urls = build_source_urls(request)
        self.assertEqual(urls[0].url, "https://www.argenprop.com/departamentos/alquiler/capital-federal")
        self.assertEqual(urls[1].url, "https://www.zonaprop.com.ar/departamentos-alquiler-capital-federal.html")

    def test_applies_area_and_price_filters(self):
        filters = SearchFilters(min_price="500000", max_price="700000", min_m2="40", max_m2="50")
        self.assertTrue(matches_filters(listing(Portal.ZONAPROP, "https://www.zonaprop.com.ar/a"), filters))
        self.assertFalse(matches_filters(Listing(source=Portal.ZONAPROP, source_url="https://www.zonaprop.com.ar/b", url="https://www.zonaprop.com.ar/b", title="Monoambiente", price="800000", currency="ARS", covered_m2="30"), filters))

    def test_rejects_invalid_polygon_coordinates(self):
        with self.assertRaises(ValueError):
            SearchFilters(polygon_geojson={"type": "Polygon", "coordinates": [[[200, 0], [0, 0], [0, 1], [200, 0]]]})


if __name__ == "__main__":
    unittest.main()
