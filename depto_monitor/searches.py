from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
import logging
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .dedupe import choose_representative, deduplicate
from .models import Listing, Portal, SearchFilters, SourceURL, normalize_text
from .sources import MercadoLibreCredentials, fetch_source

MAX_PORTALS = 5
MAX_RESULTS = 24
LIVE_FETCH_TIMEOUT_SECONDS = 20
logger = logging.getLogger(__name__)


class LiveSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portals: list[Portal] = Field(min_length=1, max_length=MAX_PORTALS)
    filters: SearchFilters

    @field_validator("portals")
    @classmethod
    def unique_portals(cls, portals: list[Portal]) -> list[Portal]:
        if len(set(portals)) != len(portals):
            raise ValueError("cada portal debe aparecer una sola vez")
        return portals

    @model_validator(mode="after")
    def require_location(self) -> "LiveSearchRequest":
        if not self.filters.locations:
            raise ValueError("indicá una localidad o barrio")
        if len(self.filters.locations) > 1:
            raise ValueError("la búsqueda en vivo admite una localidad por vez")
        return self


def build_source_urls(request: LiveSearchRequest) -> list[SourceURL]:
    location = _location_slug(request.filters.locations[0])
    operation = request.filters.operation
    sources = []
    for portal in request.portals:
        sources.append(SourceURL(portal=portal, url=_portal_search_url(portal, operation, location)))
    return sources


def search_live(
    request: LiveSearchRequest,
    *,
    mercado_libre_credentials: MercadoLibreCredentials | None = None,
) -> dict[str, Any]:
    sources = build_source_urls(request)
    listings: list[Listing] = []
    unavailable_portals: list[str] = []
    source_diagnostics: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        pending = {
            executor.submit(
                fetch_source,
                source,
                filters=request.filters,
                mercado_libre_credentials=mercado_libre_credentials,
                timeout_seconds=LIVE_FETCH_TIMEOUT_SECONDS,
            ): (source, perf_counter())
            for source in sources
        }
        for future in as_completed(pending):
            source, started_at = pending[future]
            duration_ms = int((perf_counter() - started_at) * 1_000)
            try:
                source_listings = future.result()
                listings.extend(source_listings)
                matching_count = sum(matches_filters(listing, request.filters) for listing in source_listings)
                status = "ok" if source_listings else "empty"
                source_diagnostics[source.portal.value] = {
                    "portal": source.portal.value,
                    "status": status,
                    "listing_count": len(source_listings),
                    "matching_count": matching_count,
                    "duration_ms": duration_ms,
                }
                logger.warning(
                    "live_source_finished portal=%s status=%s listing_count=%s matching_count=%s duration_ms=%s",
                    source.portal.value,
                    status,
                    len(source_listings),
                    matching_count,
                    duration_ms,
                )
            except Exception as error:
                unavailable_portals.append(source.portal.value)
                http_status = getattr(getattr(error, "response", None), "status_code", None)
                stage = getattr(error, "stage", "fetch")
                source_diagnostics[source.portal.value] = {
                    "portal": source.portal.value,
                    "status": "unavailable",
                    "listing_count": 0,
                    "duration_ms": duration_ms,
                }
                logger.warning(
                    "live_source_failed portal=%s stage=%s error_type=%s http_status=%s duration_ms=%s",
                    source.portal.value,
                    stage,
                    type(error).__name__,
                    http_status if isinstance(http_status, int) else "none",
                    duration_ms,
                )
    filtered = [listing for listing in listings if matches_filters(listing, request.filters)]
    representatives = [choose_representative(group) for group in deduplicate(filtered)]
    visible_listings = representatives[:MAX_RESULTS]
    return {
        "found": len(representatives),
        "listings": [listing.model_dump(mode="json") for listing in visible_listings],
        "source_urls": [source.model_dump(mode="json") for source in sources],
        "unavailable_portals": unavailable_portals,
        "source_diagnostics": [source_diagnostics[source.portal.value] for source in sources],
    }


def matches_filters(listing: Listing, filters: SearchFilters) -> bool:
    if filters.locations and not _matches_locations(listing, filters.locations):
        return False
    if listing.currency is not None and listing.currency != filters.currency:
        return False
    if not _between(listing.price, filters.min_price, filters.max_price):
        return False
    if not _between(listing.rooms, filters.min_rooms, filters.max_rooms):
        return False
    area = listing.covered_m2 or listing.total_m2
    if not _between(area, filters.min_m2, filters.max_m2):
        return False
    if filters.max_expenses is not None and (listing.expenses is None or listing.expenses > filters.max_expenses):
        return False
    if filters.pets_allowed is True and "mascota" not in (listing.title + " " + (listing.address or "")).lower():
        return False
    if filters.parking is True and not any(term in (listing.title + " " + (listing.address or "")).lower() for term in ("cochera", "garage", "garaje")):
        return False
    return _inside_polygon(listing, filters.polygon_geojson)


def _matches_locations(listing: Listing, locations: list[str]) -> bool:
    listing_text = normalize_text(f"{listing.title} {listing.address or ''}")
    return any(any(term in listing_text for term in _location_terms(location)) for location in locations)


def _location_terms(location: str) -> tuple[str, ...]:
    normalized = _normalized_location(location)
    aliases = {
        "caba": ("caba", "capital federal", "ciudad de buenos aires"),
        "capital federal": ("caba", "capital federal", "ciudad de buenos aires"),
        "ciudad de buenos aires": ("caba", "capital federal", "ciudad de buenos aires"),
    }
    return aliases.get(normalized, (normalized,))


def _normalized_location(location: str) -> str:
    return normalize_text(location)


def _between(value: Decimal | int | None, minimum: Decimal | int | None, maximum: Decimal | int | None) -> bool:
    if minimum is not None and (value is None or value < minimum):
        return False
    return maximum is None or (value is not None and value <= maximum)


def _inside_polygon(listing: Listing, polygon: dict[str, Any] | None) -> bool:
    if polygon is None:
        return True
    if listing.latitude is None or listing.longitude is None:
        return False
    coordinates = polygon.get("coordinates") if isinstance(polygon, dict) else None
    if polygon.get("type") == "Polygon":
        return _point_in_ring(float(listing.longitude), float(listing.latitude), coordinates[0]) if _valid_polygon_coordinates(coordinates) else False
    if polygon.get("type") == "MultiPolygon" and isinstance(coordinates, list):
        return any(_valid_polygon_coordinates(item) and _point_in_ring(float(listing.longitude), float(listing.latitude), item[0]) for item in coordinates)
    return False


def _valid_polygon_coordinates(coordinates: Any) -> bool:
    return isinstance(coordinates, list) and bool(coordinates) and isinstance(coordinates[0], list) and len(coordinates[0]) >= 4


def _point_in_ring(longitude: float, latitude: float, ring: list[list[float]]) -> bool:
    inside = False
    previous_longitude, previous_latitude = ring[-1]
    for current_longitude, current_latitude in ring:
        crosses = (current_latitude > latitude) != (previous_latitude > latitude)
        if crosses and longitude < (previous_longitude - current_longitude) * (latitude - current_latitude) / (previous_latitude - current_latitude) + current_longitude:
            inside = not inside
        previous_longitude, previous_latitude = current_longitude, current_latitude
    return inside


def _location_slug(location: str) -> str:
    normalized = unicodedata.normalize("NFKD", location).encode("ascii", "ignore").decode("ascii").lower().strip()
    aliases = {"caba": "capital-federal", "ciudad de buenos aires": "capital-federal", "buenos aires": "capital-federal"}
    slug = aliases.get(normalized, re.sub(r"[^a-z0-9]+", "-", normalized).strip("-"))
    if not slug:
        raise ValueError("la localidad no es válida")
    return slug


def _portal_search_url(portal: Portal, operation: str, location: str) -> str:
    if portal is Portal.ARGENPROP:
        return f"https://www.argenprop.com/departamentos/{operation}/{location}"
    if portal is Portal.ZONAPROP:
        return f"https://www.zonaprop.com.ar/departamentos-{operation}-{location}.html"
    if portal is Portal.MERCADOLIBRE:
        return f"https://inmuebles.mercadolibre.com.ar/departamentos/{operation}/{location}"
    if portal is Portal.ROOMIX:
        roomix_operation = "alquilar" if operation == "alquiler" else "comprar"
        return f"https://roomix.ai/buscar/{roomix_operation}/departamentos/en-{location}"
    remax_operation = "rent" if operation == "alquiler" else "sale"
    return f"https://www.remax.com.ar/listings/{remax_operation}/{location}"
