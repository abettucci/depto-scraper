import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from api.preview import preview_listings, static_asset_for
from depto_monitor.dedupe import deduplicate
from depto_monitor.models import Listing, Portal, SearchFilters, SourceURL
from depto_monitor.searches import LiveSearchRequest, build_source_urls, filter_rejection_reason, matches_filters
from depto_monitor.sources import MercadoLibreCredentials, fetch_source, parse_roomix_search_html
from depto_monitor.whatsapp import format_summary
from lambda_function import RuntimeSettings, handler as lambda_handler


def listing(source: Portal, url: str) -> Listing:
    return Listing(source=source, source_url=url, url=url, title="2 amb Palermo", address="Costa Rica 5000", price="600000", currency="ARS", rooms="2", covered_m2="45")


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.encoding = "utf-8"
        self._content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("request failed")

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self._content


class FakeSession:
    def __init__(self, post_payload: dict | None = None, get_payloads: list[dict] | None = None):
        self.post_payload = post_payload or {}
        self.get_payloads = list(get_payloads or [])
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse(self.post_payload)

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return FakeResponse(self.get_payloads.pop(0))


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
        self.assertEqual(result["source_diagnostics"][0]["status"], "ok")
        self.assertEqual(result["source_diagnostics"][0]["listing_count"], 1)

    def test_builds_sources_from_portals_and_location(self):
        request = LiveSearchRequest.model_validate({"portals": ["argenprop", "zonaprop", "roomix"], "filters": {"locations": ["CABA"]}})
        urls = build_source_urls(request)
        self.assertEqual(urls[0].url, "https://www.argenprop.com/departamentos/alquiler/capital-federal")
        self.assertEqual(urls[1].url, "https://www.zonaprop.com.ar/departamentos-alquiler-capital-federal.html")
        self.assertEqual(urls[2].url, "https://roomix.ai/buscar/alquilar/departamentos/en-capital-federal")

    def test_builds_sources_for_multiple_locations(self):
        request = LiveSearchRequest.model_validate({"portals": ["zonaprop", "remax"], "filters": {"locations": ["Palermo", "Belgrano"]}})
        urls = build_source_urls(request)
        self.assertEqual(len(urls), 4)
        self.assertEqual(urls[0].url, "https://www.zonaprop.com.ar/departamentos-alquiler-palermo.html")
        self.assertEqual(urls[3].url, "https://www.remax.com.ar/listings/rent/belgrano")

    def test_rejects_too_many_live_source_combinations(self):
        with self.assertRaises(ValueError):
            LiveSearchRequest.model_validate({"portals": ["argenprop", "zonaprop", "mercadolibre", "remax", "roomix"], "filters": {"locations": ["Palermo", "Belgrano", "Núñez", "Saavedra", "Colegiales"]}})

    def test_applies_area_and_price_filters(self):
        filters = SearchFilters(min_price="500000", max_price="700000", min_m2="40", max_m2="50")
        self.assertTrue(matches_filters(listing(Portal.ZONAPROP, "https://www.zonaprop.com.ar/a"), filters))
        self.assertFalse(matches_filters(Listing(source=Portal.ZONAPROP, source_url="https://www.zonaprop.com.ar/b", url="https://www.zonaprop.com.ar/b", title="Monoambiente", price="800000", currency="ARS", covered_m2="30"), filters))

    def test_requires_more_than_twenty_square_meters_as_minimum(self):
        with self.assertRaises(ValueError):
            SearchFilters(min_m2="20")
        self.assertEqual(SearchFilters(min_m2="21").min_m2, Decimal("21"))

    def test_allows_room_filters_above_twenty(self):
        filters = SearchFilters(min_rooms=40, max_rooms=50)
        self.assertEqual(filters.min_rooms, 40)
        self.assertEqual(filters.max_rooms, 50)

    def test_applies_location_filter_to_extracted_listings(self):
        filters = SearchFilters(locations=["Palermo"])
        self.assertTrue(matches_filters(listing(Portal.ZONAPROP, "https://www.zonaprop.com.ar/a"), filters))
        self.assertFalse(matches_filters(Listing(source=Portal.ZONAPROP, source_url="https://www.zonaprop.com.ar/b", url="https://www.zonaprop.com.ar/b", title="2 amb", address="Caballito", price="600000", currency="ARS"), filters))

    def test_explains_filter_rejection_without_listing_data(self):
        filtered_listing = Listing(source=Portal.ZONAPROP, source_url="https://www.zonaprop.com.ar/b", url="https://www.zonaprop.com.ar/b", title="2 amb", address="Caballito", price="600000", currency="ARS")
        self.assertEqual(filter_rejection_reason(filtered_listing, SearchFilters(locations=["Palermo"])), "location")

    def test_fetches_mercado_libre_with_official_api(self):
        session = FakeSession(
            post_payload={"access_token": "a" * 32, "refresh_token": "n" * 32, "expires_in": 10_800},
            get_payloads=[
                {
                    "results": [
                        {
                            "title": "Departamento 2 ambientes Palermo",
                            "permalink": "https://departamento.mercadolibre.com.ar/MLA-123-test",
                            "price": 650000,
                            "currency_id": "ARS",
                            "seller_address": {"address_line": "Costa Rica 5000", "neighborhood": {"name": "Palermo"}, "city": {"name": "Capital Federal"}},
                            "attributes": [
                                {"id": "ROOMS", "value_name": "2"},
                                {"id": "COVERED_AREA", "value_struct": {"number": 45}},
                            ],
                        }
                    ]
                }
            ],
        )
        source = SourceURL(portal=Portal.MERCADOLIBRE, url="https://inmuebles.mercadolibre.com.ar/departamentos/alquiler/palermo")
        credentials = MercadoLibreCredentials(client_id="123", client_secret="s" * 16, refresh_token="r" * 32)
        listings = fetch_source(
            source,
            filters=SearchFilters(locations=["Palermo"]),
            mercado_libre_credentials=credentials,
            session=session,
        )

        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].rooms, 2)
        self.assertEqual(listings[0].covered_m2, 45)
        self.assertEqual(session.get_calls[0][1]["params"]["category"], "MLA1473")
        self.assertEqual(session.get_calls[0][1]["headers"]["Authorization"], f"Bearer {'a' * 32}")
        self.assertTrue(credentials.token_refreshed)
        self.assertEqual(credentials.refresh_token.get_secret_value(), "n" * 32)

    def test_uses_departments_sale_category_for_mercado_libre(self):
        session = FakeSession(
            post_payload={"access_token": "a" * 32, "refresh_token": "n" * 32, "expires_in": 10_800},
            get_payloads=[{"results": []}],
        )
        source = SourceURL(portal=Portal.MERCADOLIBRE, url="https://inmuebles.mercadolibre.com.ar/departamentos/venta/palermo")
        credentials = MercadoLibreCredentials(client_id="123", client_secret="s" * 16, refresh_token="r" * 32)

        fetch_source(
            source,
            filters=SearchFilters(operation="venta", locations=["Palermo"]),
            mercado_libre_credentials=credentials,
            session=session,
        )

        self.assertEqual(session.get_calls[0][1]["params"]["category"], "MLA1474")

    def test_reuses_valid_mercado_libre_access_token(self):
        credentials = MercadoLibreCredentials(
            client_id="123",
            client_secret="s" * 16,
            refresh_token="r" * 32,
            access_token="a" * 32,
            access_token_expires_at=4_102_444_800,
        )

        self.assertEqual(credentials.usable_access_token(), "a" * 32)
        self.assertFalse(credentials.token_refreshed)

    def test_fetches_zonaprop_postings_api(self):
        session = FakeSession(
            post_payload={
                "listPostings": [
                    {
                        "title": "Departamento 2 ambientes en Palermo",
                        "url": "/propiedades/clasificado/alclapin-departamento-palermo-123.html",
                        "priceOperationTypes": [{"prices": [{"amount": 650000, "currency": "ARS"}]}],
                        "mainFeatures": {
                            "CFT1": {"value": 2},
                            "CFT2": {"value": 1},
                            "CFT100": {"value": 52},
                            "CFT101": {"value": 45},
                        },
                        "postingLocation": {
                            "address": {"name": "Costa Rica 5000"},
                            "location": {"name": "Palermo", "parent": {"name": "Capital Federal"}},
                            "geolocation": {"latitude": -34.58, "longitude": -58.43},
                        },
                    }
                ]
            }
        )
        source = SourceURL(portal=Portal.ZONAPROP, url="https://www.zonaprop.com.ar/departamentos-alquiler-palermo.html")

        listings = fetch_source(source, filters=SearchFilters(locations=["Palermo"]), session=session)

        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].url, "https://www.zonaprop.com.ar/propiedades/clasificado/alclapin-departamento-palermo-123.html")
        self.assertEqual(listings[0].covered_m2, 45)
        self.assertEqual(listings[0].rooms, 2)
        self.assertEqual(session.post_calls[0][0], "https://www.zonaprop.com.ar/rplis-api/postings")
        payload = session.post_calls[0][1]["json"]
        self.assertEqual(payload["city"], "1004884")
        self.assertEqual(payload["tipoDeOperacion"], "2")
        self.assertNotIn("headers", session.post_calls[0][1])

    def test_parses_roomix_search_cards(self):
        source = SourceURL(portal=Portal.ROOMIX, url="https://roomix.ai/buscar/alquilar/departamentos/en-palermo")
        html = """
        <a href="/propiedad/departamento-palermo-123abc">
          <span>Lavalle 3916, Palermo</span><span>$700.000 + $ 100.000 exp</span><span>2 amb 1 hab 31 m²</span>
        </a>
        """

        listings = parse_roomix_search_html(html, source)

        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].source, Portal.ROOMIX)
        self.assertEqual(listings[0].address, "Lavalle 3916, Palermo")
        self.assertEqual(listings[0].price, 700000)
        self.assertEqual(listings[0].expenses, 100000)
        self.assertEqual(listings[0].rooms, 2)
        self.assertEqual(listings[0].covered_m2, 31)

    def test_fetches_remax_json_endpoint(self):
        session = FakeSession(
            get_payloads=[
                {
                    "data": {
                        "data": [
                            {
                                "id": 42,
                                "slug": "alquiler-departamento-palermo",
                                "title": "Alquiler departamento Palermo",
                                "displayAddress": "Costa Rica 5000",
                                "geoLabel": "Palermo, Capital Federal",
                                "location": {"coordinates": [-58.43, -34.58]},
                                "price": 700000,
                                "currency": {"value": "ARS"},
                                "expensesPrice": 50000,
                                "expensesCurrency": {"value": "ARS"},
                                "totalRooms": 2,
                                "bedrooms": 1,
                                "dimensionCovered": 44,
                                "dimensionTotalBuilt": 50,
                            }
                        ],
                        "totalPages": 1,
                    }
                }
            ]
        )
        source = SourceURL(portal=Portal.REMAX, url="https://www.remax.com.ar/listings/rent/palermo")
        listings = fetch_source(source, filters=SearchFilters(locations=["Palermo"]), session=session)

        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].url, "https://www.remax.com.ar/listings/alquiler-departamento-palermo")
        self.assertEqual(listings[0].latitude, Decimal("-34.58"))
        self.assertEqual(listings[0].longitude, Decimal("-58.43"))
        self.assertIn(("in", "operationId:2"), session.get_calls[0][1]["params"])

    def test_rejects_invalid_polygon_coordinates(self):
        with self.assertRaises(ValueError):
            SearchFilters(polygon_geojson={"type": "Polygon", "coordinates": [[[200, 0], [0, 0], [0, 1], [200, 0]]]})

    def test_serves_only_allowlisted_static_assets(self):
        asset = static_asset_for("/?source=vercel")
        self.assertIsNotNone(asset)
        self.assertEqual(asset[0].name, "index.html")
        self.assertIsNone(static_asset_for("/../../.env"))

    def test_runtime_settings_accept_empty_optional_whatsapp_values(self):
        settings = RuntimeSettings(
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="x" * 20,
            preview_shared_secret="s" * 32,
            whatsapp_sidecar_url="",
            waweb_shared_secret="   ",
            default_whatsapp_chat_id="",
        )

        self.assertIsNone(settings.whatsapp_sidecar_url)
        self.assertIsNone(settings.waweb_shared_secret)
        self.assertIsNone(settings.default_whatsapp_chat_id)

    @patch("lambda_function.search_live")
    @patch("lambda_function.load_runtime_settings")
    def test_lambda_preview_requires_secret_and_returns_results(self, load_runtime_settings, search_live):
        load_runtime_settings.return_value = RuntimeSettings(
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="x" * 20,
            preview_shared_secret="s" * 32,
        )
        search_live.return_value = {"found": 0, "listings": []}
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "headers": {"authorization": f"Bearer {'s' * 32}"},
            "body": '{"portals":["zonaprop"],"filters":{"locations":["Palermo"]}}',
            "isBase64Encoded": False,
        }

        response = lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["found"], 0)
        event["headers"] = {}
        self.assertEqual(lambda_handler(event, None)["statusCode"], 401)


if __name__ == "__main__":
    unittest.main()
