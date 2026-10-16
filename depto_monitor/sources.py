from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin

import requests

from .models import Listing, SourceURL, validate_portal_url

REQUEST_TIMEOUT_SECONDS = 25
MAX_RESPONSE_BYTES = 2_000_000
JSON_LD_SCRIPT = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)


def fetch_source(source: SourceURL, session: requests.Session | None = None) -> list[Listing]:
    client = session or requests.Session()
    response = client.get(source.url, headers={"User-Agent": "depto-monitor/1.0 (personal search monitor)"}, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False, stream=True)
    response.raise_for_status()
    if response.headers.get("content-type", "").split(";", 1)[0].lower() not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("la fuente no devolvio HTML")
    payload = bytearray()
    for chunk in response.iter_content(chunk_size=32_768):
        payload.extend(chunk)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("la respuesta excede el limite permitido")
    return parse_json_ld(payload.decode(response.encoding or "utf-8", errors="replace"), source)


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
    title = str(item.get("name") or item.get("headline") or "").strip()
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
    return Listing(source=source.portal, source_url=source.url, url=url, title=title, address=str(address) if address else None, price=_value_or_none(offer.get("price")), currency=_currency(offer.get("priceCurrency")), rooms=_value_or_none(item.get("numberOfRooms")), bedrooms=_value_or_none(item.get("numberOfBedrooms")), covered_m2=_value_or_none(item.get("floorSize", {}).get("value") if isinstance(item.get("floorSize"), dict) else None), latitude=_value_or_none(geo.get("latitude")), longitude=_value_or_none(geo.get("longitude")), image_url=str(image) if image else None)


def _value_or_none(value: Any):
    return None if value in (None, "") else str(value)


def _currency(value: Any) -> str | None:
    value = str(value).upper() if value else ""
    return value if value in {"ARS", "USD"} else None
