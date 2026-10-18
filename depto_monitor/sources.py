from __future__ import annotations

import json
import math
import re
from html import unescape
from html.parser import HTMLParser
from time import time
from typing import Any
from urllib.parse import urljoin

import requests
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr

from .models import Listing, Portal, SearchFilters, SourceURL, normalize_text, validate_portal_url

REQUEST_TIMEOUT_SECONDS = 25
MAX_RESPONSE_BYTES = 2_000_000
MAX_MELI_RESULTS = 50
MAX_REMAX_PAGES = 2
MAX_REMAX_PAGE_SIZE = 50
MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
MELI_SEARCH_URL = "https://api.mercadolibre.com/sites/MLA/search"
MELI_REAL_ESTATE_CATEGORY = "MLA1459"
REMAX_LISTINGS_URL = "https://api-ar.redremax.com/remaxweb-ar/api/listings/findAllWithEntrepreneurships"
ROOMIX_PROPERTY_PATH = re.compile(r"^/propiedad/[a-z0-9-]+$", re.IGNORECASE)
ROOMIX_PRICE = re.compile(r"(?P<currency>USD|U\$S|US\$|ARS|\$)\s*(?P<amount>\d[\d.,]*)", re.IGNORECASE)
ROOMIX_EXPENSES = re.compile(r"\+\s*\$\s*(?P<amount>\d[\d.,]*)\s*(?:exp|expensas)", re.IGNORECASE)
ROOMIX_ROOMS = re.compile(r"(?P<rooms>\d+)\s*amb\b|(?P<studio>monoambiente)", re.IGNORECASE)
ROOMIX_BEDROOMS = re.compile(r"(?P<bedrooms>\d+)\s*hab\b", re.IGNORECASE)
ROOMIX_AREA = re.compile(r"(?P<area>\d[\d.,]*)\s*m²", re.IGNORECASE)
ZONAPROP_POSTINGS_URL = "https://www.zonaprop.com.ar/rplis-api/postings"
ZONAPROP_CABA_LOCATION_ID = "1004884"
ZONAPROP_CABA_LOCATIONS = frozenset(
    {
        "caba",
        "capital federal",
        "ciudad de buenos aires",
        "almagro",
        "balvanera",
        "barracas",
        "belgrano",
        "caballito",
        "chacarita",
        "colegiales",
        "coghlan",
        "flores",
        "floresta",
        "la boca",
        "monserrat",
        "nunez",
        "palermo",
        "puerto madero",
        "recoleta",
        "retiro",
        "san cristobal",
        "san nicolas",
        "villa crespo",
        "villa devoto",
        "villa urquiza",
    }
)
JSON_LD_SCRIPT = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)


class SourceUnavailableError(RuntimeError):
    """The configured source cannot be queried in the current runtime."""


class SourceHTTPError(RuntimeError):
    def __init__(self, stage: str, response: requests.Response):
        super().__init__("la fuente respondió con un error HTTP")
        self.stage = stage
        self.response = response


class _RoomixSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self._href is not None:
            return
        href = dict(attrs).get("href")
        if isinstance(href, str) and ROOMIX_PROPERTY_PATH.fullmatch(href):
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        text = " ".join(part.strip() for part in self._text if part.strip())
        if text:
            self.entries.append((self._href, text))
        self._href = None
        self._text = []


class MercadoLibreCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=256)
    client_secret: SecretStr = Field(min_length=8, max_length=4096)
    refresh_token: SecretStr = Field(min_length=16, max_length=4096)
    access_token: SecretStr | None = Field(default=None, min_length=16, max_length=4096)
    access_token_expires_at: int | None = Field(default=None, ge=0, le=4_102_444_800)
    _token_refreshed: bool = PrivateAttr(default=False)

    def usable_access_token(self) -> str | None:
        if self.access_token is None or self.access_token_expires_at is None:
            return None
        return self.access_token.get_secret_value() if self.access_token_expires_at > int(time()) + 60 else None

    def update_tokens(self, access_token: str, refresh_token: str, expires_in: int) -> None:
        self.access_token = SecretStr(access_token)
        self.refresh_token = SecretStr(refresh_token)
        self.access_token_expires_at = int(time()) + expires_in
        self._token_refreshed = True

    @property
    def token_refreshed(self) -> bool:
        return self._token_refreshed


def fetch_source(
    source: SourceURL,
    *,
    filters: SearchFilters | None = None,
    mercado_libre_credentials: MercadoLibreCredentials | None = None,
    session: requests.Session | None = None,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
) -> list[Listing]:
    client = session or requests.Session()
    if source.portal is Portal.MERCADOLIBRE:
        if filters is None or mercado_libre_credentials is None:
            raise SourceUnavailableError("Mercado Libre requiere credenciales runtime")
        return _fetch_mercado_libre(source, filters, mercado_libre_credentials, client, timeout_seconds)
    if source.portal is Portal.REMAX:
        if filters is None:
            raise SourceUnavailableError("RE/MAX requiere filtros de búsqueda")
        return _fetch_remax(source, filters, client, timeout_seconds)
    if source.portal is Portal.ZONAPROP:
        if filters is None:
            raise SourceUnavailableError("Zonaprop requiere filtros de búsqueda")
        return _fetch_zonaprop(source, filters, client, timeout_seconds)
    if source.portal is Portal.ROOMIX:
        return _fetch_roomix(source, client, timeout_seconds)
    return _fetch_html_json_ld(source, client, timeout_seconds)


def _fetch_html_json_ld(source: SourceURL, client: requests.Session, timeout_seconds: int) -> list[Listing]:
    response = client.get(source.url, timeout=timeout_seconds, allow_redirects=False, stream=True)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("la fuente no devolvió HTML")
    payload = _read_limited_response(response)
    return parse_json_ld(payload.decode(response.encoding or "utf-8", errors="replace"), source)


def _fetch_mercado_libre(
    source: SourceURL,
    filters: SearchFilters,
    credentials: MercadoLibreCredentials,
    client: requests.Session,
    timeout_seconds: int,
) -> list[Listing]:
    access_token = credentials.usable_access_token()
    if access_token is None:
        token_response = client.post(
            MELI_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret.get_secret_value(),
                "refresh_token": credentials.refresh_token.get_secret_value(),
            },
            timeout=timeout_seconds,
            allow_redirects=False,
            stream=True,
        )
        access_token = _refresh_mercado_libre_tokens(token_response, credentials)
    response = client.get(
        MELI_SEARCH_URL,
        params=_mercado_libre_params(filters),
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout_seconds,
        allow_redirects=False,
        stream=True,
    )
    payload = _read_json_response(response, stage="mercadolibre_search")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("la respuesta de Mercado Libre no tiene resultados válidos")
    listings = [_parse_mercado_libre_listing(item, source) for item in raw_results]
    return [listing for listing in listings if listing is not None]


def _refresh_mercado_libre_tokens(response: requests.Response, credentials: MercadoLibreCredentials) -> str:
    payload = _read_json_response(response, stage="mercadolibre_oauth")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or len(access_token) < 16 or not isinstance(refresh_token, str) or len(refresh_token) < 16 or not isinstance(expires_in, int) or not 60 <= expires_in <= 86_400:
        raise ValueError("Mercado Libre no devolvió un token válido")
    credentials.update_tokens(access_token, refresh_token, expires_in)
    return access_token


def _mercado_libre_params(filters: SearchFilters) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "category": MELI_REAL_ESTATE_CATEGORY,
        "limit": MAX_MELI_RESULTS,
    }
    location = filters.locations[0] if filters.locations else ""
    if location:
        params["q"] = location
    bounding_box = _filter_bounding_box(filters)
    if bounding_box is not None:
        south, north, west, east = bounding_box
        params["item_location"] = f"lat:{south}_{north},lon:{west}_{east}"
    return params


def _fetch_remax(source: SourceURL, filters: SearchFilters, client: requests.Session, timeout_seconds: int) -> list[Listing]:
    listings: list[Listing] = []
    seen_urls: set[str] = set()
    for page in range(MAX_REMAX_PAGES):
        response = client.get(
            REMAX_LISTINGS_URL,
            params=_remax_params(filters, page),
            timeout=timeout_seconds,
            allow_redirects=False,
            stream=True,
        )
        payload = _read_json_response(response, stage="remax_listings")
        data = payload.get("data")
        raw_listings = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw_listings, list):
            raise ValueError("la respuesta de RE/MAX no tiene resultados válidos")
        for item in raw_listings:
            listing = _parse_remax_listing(item, source)
            if listing is not None and listing.url not in seen_urls:
                listings.append(listing)
                seen_urls.add(listing.url)
        total_pages = data.get("totalPages") if isinstance(data, dict) else 0
        if not raw_listings or not isinstance(total_pages, int) or page + 1 >= total_pages:
            break
    return listings


def _fetch_zonaprop(source: SourceURL, filters: SearchFilters, client: requests.Session, timeout_seconds: int) -> list[Listing]:
    response = client.post(
        ZONAPROP_POSTINGS_URL,
        json=_zonaprop_payload(filters),
        timeout=timeout_seconds,
        allow_redirects=False,
        stream=True,
    )
    payload = _read_json_response(response, stage="zonaprop_postings")
    raw_listings = payload.get("listPostings")
    if not isinstance(raw_listings, list):
        raise ValueError("la respuesta de Zonaprop no tiene avisos válidos")
    listings = [_parse_zonaprop_listing(item, source) for item in raw_listings]
    return [listing for listing in listings if listing is not None]


def _fetch_roomix(source: SourceURL, client: requests.Session, timeout_seconds: int) -> list[Listing]:
    response = client.get(source.url, timeout=timeout_seconds, allow_redirects=False, stream=True)
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise SourceHTTPError("roomix_search", response) from error
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("la fuente no devolvió HTML")
    return parse_roomix_search_html(_read_limited_response(response).decode(response.encoding or "utf-8", errors="replace"), source)


def parse_roomix_search_html(html: str, source: SourceURL) -> list[Listing]:
    parser = _RoomixSearchParser()
    parser.feed(html)
    parser.close()
    listings = [_parse_roomix_listing(path, text, source) for path, text in parser.entries]
    return list({listing.source_key: listing for listing in listings if listing is not None}.values())


def _zonaprop_payload(filters: SearchFilters) -> dict[str, str | int]:
    operation = "2" if filters.operation == "alquiler" else "1"
    payload: dict[str, str | int] = {
        "moneda": 1 if filters.currency == "ARS" else 2,
        "preciomin": str(filters.min_price or 0),
        "preciomax": str(filters.max_price or 99_999_999_999),
        "tipoDeOperacion": operation,
        "preTipoDeOperacion": operation,
        "superficieCubierta": 1,
        "idunidaddemedida": 1,
        "tipoAnunciante": "ALL",
        "sort": "relevance",
    }
    location = normalize_text(filters.locations[0]) if filters.locations else ""
    if location in ZONAPROP_CABA_LOCATIONS:
        payload["city"] = ZONAPROP_CABA_LOCATION_ID
    return payload


def _remax_params(filters: SearchFilters, page: int) -> list[tuple[str, str | int]]:
    operation_id = 2 if filters.operation == "alquiler" else 1
    params: list[tuple[str, str | int]] = [
        ("sort", "-createdAt"),
        ("in", f"operationId:{operation_id}"),
        ("in", "typeId:1,2,3,4,5,6,7,8"),
        ("pageSize", MAX_REMAX_PAGE_SIZE),
        ("page", page),
    ]
    if filters.min_m2 is not None:
        params.append(("gte", f"dimensionCovered:{filters.min_m2}"))
    if filters.max_m2 is not None:
        params.append(("lte", f"dimensionCovered:{filters.max_m2}"))
    return params


def _read_json_response(response: requests.Response, *, stage: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise SourceHTTPError(stage, response) from error
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"application/json", "application/problem+json"}:
        raise ValueError("la fuente no devolvió JSON")
    try:
        payload = json.loads(_read_limited_response(response).decode(response.encoding or "utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("la fuente devolvió JSON inválido") from error
    if not isinstance(payload, dict):
        raise ValueError("la fuente devolvió JSON inválido")
    return payload


def _read_limited_response(response: requests.Response) -> bytes:
    payload = bytearray()
    for chunk in response.iter_content(chunk_size=32_768):
        payload.extend(chunk)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("la respuesta excede el límite permitido")
    return bytes(payload)


def _parse_mercado_libre_listing(item: Any, source: SourceURL) -> Listing | None:
    if not isinstance(item, dict):
        return None
    url = item.get("permalink")
    title = _string(item.get("title"))
    if not title or not isinstance(url, str):
        return None
    attributes = _attributes_by_id(item.get("attributes"))
    address = _mercado_libre_address(item)
    latitude, longitude = _coordinates(item.get("location"), item.get("seller_address"))
    try:
        return Listing(
            source=Portal.MERCADOLIBRE,
            source_url=source.url,
            url=url,
            title=title,
            address=address,
            price=item.get("price"),
            currency=_currency(item.get("currency_id")),
            expenses=_attribute_value(attributes, "MAINTENANCE_FEE", "EXPENSES", "EXPENSAS"),
            rooms=_attribute_value(attributes, "ROOMS", "TOTAL_ROOMS", "AMBIENTES"),
            bedrooms=_attribute_value(attributes, "BEDROOMS", "DORMITORY", "DORMITORIOS"),
            covered_m2=_attribute_value(attributes, "COVERED_AREA", "COVERED_M2", "SUPERFICIE_CUBIERTA"),
            total_m2=_attribute_value(attributes, "TOTAL_AREA", "TOTAL_M2", "SUPERFICIE_TOTAL"),
            latitude=latitude,
            longitude=longitude,
            image_url=_https_url_or_none(item.get("thumbnail")),
        )
    except ValueError:
        return None


def _parse_remax_listing(item: Any, source: SourceURL) -> Listing | None:
    if not isinstance(item, dict):
        return None
    title = _string(item.get("title")) or _string(item.get("displayAddress"))
    slug = _string(item.get("slug"))
    listing_id = _string(item.get("listingId")) or _string(item.get("id"))
    if not title or not (slug or listing_id):
        return None
    raw_url = _string(item.get("url"))
    if raw_url:
        url = raw_url if raw_url.startswith("https://") else urljoin("https://www.remax.com.ar/", raw_url)
    elif slug:
        url = urljoin("https://www.remax.com.ar/listings/", slug)
    else:
        url = f"https://www.remax.com.ar/listings/{listing_id}"
    coordinates = item.get("location", {}).get("coordinates") if isinstance(item.get("location"), dict) else None
    longitude, latitude = _point_coordinates(coordinates)
    currency = item.get("currency")
    expenses_currency = item.get("expensesCurrency")
    try:
        return Listing(
            source=Portal.REMAX,
            source_url=source.url,
            url=url,
            title=title,
            address=_string(item.get("displayAddress")) or _string(item.get("geoLabel")),
            price=item.get("price"),
            currency=_currency(currency.get("value") if isinstance(currency, dict) else None),
            expenses=item.get("expensesPrice") if _currency(expenses_currency.get("value") if isinstance(expenses_currency, dict) else None) == "ARS" else None,
            rooms=item.get("totalRooms"),
            bedrooms=item.get("bedrooms"),
            covered_m2=item.get("dimensionCovered"),
            total_m2=item.get("dimensionTotalBuilt"),
            latitude=latitude,
            longitude=longitude,
            image_url=None,
        )
    except ValueError:
        return None


def _parse_zonaprop_listing(item: Any, source: SourceURL) -> Listing | None:
    if not isinstance(item, dict):
        return None
    title = _string(item.get("title"))
    raw_url = _string(item.get("url"))
    if not title or not raw_url:
        return None
    url = urljoin("https://www.zonaprop.com.ar/", raw_url)
    try:
        validate_portal_url(url, Portal.ZONAPROP)
    except ValueError:
        return None
    prices = _first_zonaprop_prices(item)
    main_features = item.get("mainFeatures") if isinstance(item.get("mainFeatures"), dict) else {}
    location = item.get("postingLocation") if isinstance(item.get("postingLocation"), dict) else {}
    latitude, longitude = _zonaprop_coordinates(location)
    try:
        return Listing(
            source=Portal.ZONAPROP,
            source_url=source.url,
            url=url,
            title=title,
            address=_zonaprop_address(location),
            price=prices.get("amount"),
            currency=_zonaprop_currency(prices.get("currency")),
            expenses=prices.get("expenses"),
            rooms=_zonaprop_feature_value(main_features, "CFT1"),
            bedrooms=_zonaprop_feature_value(main_features, "CFT2"),
            covered_m2=_zonaprop_feature_value(main_features, "CFT101"),
            total_m2=_zonaprop_feature_value(main_features, "CFT100"),
            latitude=latitude,
            longitude=longitude,
        )
    except ValueError:
        return None


def _parse_roomix_listing(path: str, text: str, source: SourceURL) -> Listing | None:
    compact_text = " ".join(text.split())
    price_match = ROOMIX_PRICE.search(compact_text)
    if price_match is None:
        return None
    address = compact_text[: price_match.start()].strip(" ·|-–") or None
    if address is None:
        return None
    rooms_match = ROOMIX_ROOMS.search(compact_text)
    bedrooms_match = ROOMIX_BEDROOMS.search(compact_text)
    area_match = ROOMIX_AREA.search(compact_text)
    expenses_match = ROOMIX_EXPENSES.search(compact_text)
    try:
        return Listing(
            source=Portal.ROOMIX,
            source_url=source.url,
            url=urljoin("https://roomix.ai/", path),
            title=address,
            address=address,
            price=_decimal_text_or_none(price_match.group("amount")),
            currency=_roomix_currency(price_match.group("currency")),
            expenses=_decimal_text_or_none(expenses_match.group("amount")) if expenses_match else None,
            rooms=1 if rooms_match and rooms_match.group("studio") else _decimal_text_or_none(rooms_match.group("rooms")) if rooms_match else None,
            bedrooms=_decimal_text_or_none(bedrooms_match.group("bedrooms")) if bedrooms_match else None,
            covered_m2=_decimal_text_or_none(area_match.group("area")) if area_match else None,
        )
    except ValueError:
        return None
def parse_json_ld(html: str, source: SourceURL) -> list[Listing]:
    listings: list[Listing] = []
    for raw_script in JSON_LD_SCRIPT.findall(html):
        try:
            data = json.loads(unescape(raw_script.strip()))
        except json.JSONDecodeError:
            continue
        for item in _iter_items(data):
            parsed = _parse_item(item, source)
            if parsed is not None:
                listings.append(parsed)
    return list({listing.source_key: listing for listing in listings}.values())


def _iter_items(data: Any):
    if isinstance(data, list):
        for value in data:
            yield from _iter_items(value)
    elif isinstance(data, dict):
        item_list = data.get("itemListElement")
        if isinstance(item_list, list):
            for entry in item_list:
                if isinstance(entry, dict):
                    yield entry.get("item", entry)
        elif data.get("@type") in {"Product", "Apartment", "Residence", "Offer"}:
            yield data
        if data.get("@graph"):
            yield from _iter_items(data["@graph"])


def _parse_item(item: Any, source: SourceURL) -> Listing | None:
    if not isinstance(item, dict):
        return None
    title = _string(item.get("name")) or _string(item.get("headline"))
    raw_url = item.get("url") or item.get("@id")
    if not title or not isinstance(raw_url, str):
        return None
    url = urljoin(source.url, raw_url)
    try:
        validate_portal_url(url, source.portal)
    except ValueError:
        return None
    offer = item.get("offers") if isinstance(item.get("offers"), dict) else item
    address = item.get("address")
    if isinstance(address, dict):
        address = ", ".join(str(address.get(key)) for key in ("streetAddress", "addressLocality") if address.get(key))
    geo = item.get("geo") if isinstance(item.get("geo"), dict) else {}
    image = item.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    try:
        return Listing(
            source=source.portal,
            source_url=source.url,
            url=url,
            title=title,
            address=str(address) if address else None,
            price=_value_or_none(offer.get("price")),
            currency=_currency(offer.get("priceCurrency")),
            rooms=_value_or_none(item.get("numberOfRooms")),
            bedrooms=_value_or_none(item.get("numberOfBedrooms")),
            covered_m2=_value_or_none(item.get("floorSize", {}).get("value") if isinstance(item.get("floorSize"), dict) else None),
            latitude=_value_or_none(geo.get("latitude")),
            longitude=_value_or_none(geo.get("longitude")),
            image_url=_https_url_or_none(image),
        )
    except ValueError:
        return None


def _attributes_by_id(raw_attributes: Any) -> dict[str, Any]:
    if not isinstance(raw_attributes, list):
        return {}
    return {
        str(attribute.get("id")).upper(): attribute
        for attribute in raw_attributes
        if isinstance(attribute, dict) and isinstance(attribute.get("id"), str)
    }


def _first_zonaprop_prices(item: dict[str, Any]) -> dict[str, Any]:
    operation_types = item.get("priceOperationTypes")
    if not isinstance(operation_types, list) or not operation_types:
        return {}
    first_operation = operation_types[0]
    if not isinstance(first_operation, dict):
        return {}
    prices = first_operation.get("prices")
    if not isinstance(prices, list) or not prices or not isinstance(prices[0], dict):
        return {}
    return prices[0]


def _zonaprop_feature_value(features: dict[str, Any], feature_id: str) -> Any:
    feature = features.get(feature_id)
    return feature.get("value") if isinstance(feature, dict) else None


def _zonaprop_address(location: dict[str, Any]) -> str | None:
    address = location.get("address")
    nested_location = location.get("location")
    parts = [
        _string(address.get("name")) if isinstance(address, dict) else None,
        _string(nested_location.get("name")) if isinstance(nested_location, dict) else None,
        _string(nested_location.get("parent", {}).get("name")) if isinstance(nested_location, dict) and isinstance(nested_location.get("parent"), dict) else None,
    ]
    return ", ".join(part for part in parts if part) or None


def _zonaprop_coordinates(location: dict[str, Any]) -> tuple[Any, Any]:
    for key in ("geolocation", "geoLocation", "coordinates"):
        value = location.get(key)
        if not isinstance(value, dict):
            continue
        latitude = value.get("latitude") or value.get("lat")
        longitude = value.get("longitude") or value.get("lng")
        if latitude is not None and longitude is not None:
            return latitude, longitude
    return None, None


def _zonaprop_currency(value: Any) -> str | None:
    normalized = _string(value)
    if normalized is None:
        return None
    mapping = {
        "ars": "ARS",
        "pesos": "ARS",
        "$": "ARS",
        "usd": "USD",
        "u$s": "USD",
        "us$": "USD",
    }
    return mapping.get(normalized.lower())


def _roomix_currency(value: str) -> str:
    return "USD" if value.upper().replace("$", "") in {"USD", "US"} else "ARS"


def _decimal_text_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace(".", "").replace(",", ".")
    return normalized if normalized.replace(".", "", 1).isdigit() else None


def _attribute_value(attributes: dict[str, Any], *names: str) -> Any:
    for name in names:
        attribute = attributes.get(name)
        if isinstance(attribute, dict):
            value = attribute.get("value_struct")
            if isinstance(value, dict) and value.get("number") is not None:
                return value["number"]
            if attribute.get("value_id") is not None:
                return attribute["value_id"]
            if attribute.get("value_name") is not None:
                return attribute["value_name"]
    return None


def _mercado_libre_address(item: dict[str, Any]) -> str | None:
    seller_address = item.get("seller_address")
    if not isinstance(seller_address, dict):
        return None
    parts = [
        _string(seller_address.get("address_line")),
        _nested_name(seller_address, "neighborhood"),
        _nested_name(seller_address, "city"),
        _nested_name(seller_address, "state"),
    ]
    return ", ".join(part for part in parts if part) or None


def _nested_name(value: dict[str, Any], key: str) -> str | None:
    nested = value.get(key)
    return _string(nested.get("name")) if isinstance(nested, dict) else None


def _coordinates(*values: Any) -> tuple[Any, Any]:
    for value in values:
        if isinstance(value, dict):
            latitude = value.get("latitude")
            longitude = value.get("longitude")
            if latitude is not None and longitude is not None:
                return latitude, longitude
    return None, None


def _point_coordinates(value: Any) -> tuple[Any, Any]:
    if isinstance(value, list) and len(value) >= 2:
        return value[0], value[1]
    return None, None


def _filter_bounding_box(filters: SearchFilters) -> tuple[float, float, float, float] | None:
    if filters.polygon_geojson is not None:
        coordinates = _polygon_points(filters.polygon_geojson)
        if coordinates:
            longitudes = [point[0] for point in coordinates]
            latitudes = [point[1] for point in coordinates]
            return min(latitudes), max(latitudes), min(longitudes), max(longitudes)
    if filters.center_lat is None or filters.center_lng is None or filters.radius_km is None:
        return None
    latitude = float(filters.center_lat)
    longitude = float(filters.center_lng)
    radius = float(filters.radius_km)
    latitude_delta = radius / 111.32
    longitude_delta = radius / max(111.32 * math.cos(math.radians(latitude)), 0.01)
    return latitude - latitude_delta, latitude + latitude_delta, longitude - longitude_delta, longitude + longitude_delta


def _polygon_points(geometry: dict[str, Any]) -> list[tuple[float, float]]:
    geometry_type = geometry.get("type")
    raw_coordinates = geometry.get("coordinates")
    polygons = [raw_coordinates] if geometry_type == "Polygon" else raw_coordinates if geometry_type == "MultiPolygon" else []
    points: list[tuple[float, float]] = []
    if not isinstance(polygons, list):
        return points
    for polygon in polygons:
        if not isinstance(polygon, list):
            continue
        for ring in polygon:
            if not isinstance(ring, list):
                continue
            for point in ring:
                if isinstance(point, list) and len(point) >= 2 and isinstance(point[0], (int, float)) and isinstance(point[1], (int, float)):
                    points.append((float(point[0]), float(point[1])))
    return points


def _https_url_or_none(value: Any) -> str | None:
    url = _string(value)
    if url is None:
        return None
    return "https:" + url if url.startswith("//") else url.replace("http://", "https://", 1) if url.startswith("http://") else url


def _string(value: Any) -> str | None:
    value = str(value).strip() if value not in (None, "") else ""
    return value or None


def _value_or_none(value: Any):
    return None if value in (None, "") else str(value)


def _currency(value: Any) -> str | None:
    value = str(value).upper() if value else ""
    return value if value in {"ARS", "USD"} else None
