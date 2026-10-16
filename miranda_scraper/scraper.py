"""
Miranda Bosch scraper — extrae listings de venta en Recoleta.

Strategy:
  1. Fetch listing pages (JSON-LD) → URL + basic data for all properties
  2. Fetch each detail page (__NEXT_DATA__) → expensas, orientación, piso, estado, etc.
"""

import json
import logging
import re
import time
from typing import Optional

import requests

from common import USER_AGENT, extract_piso, has_cochera, make_session

logger = logging.getLogger(__name__)

BASE_URL = (
    "https://www.mirandabosch.com/residencial/"
    "venta-recoleta-50-150-m2-cubiertos-quicksearch-rec"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9",
}

REQUEST_DELAY = 0.8  # seconds between requests


def _fetch(session: requests.Session, url: str) -> str:
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def _get_max_page(html: str) -> int:
    matches = re.findall(r'title="Ir a la p.gina (\d+)"', html)
    return max((int(m) for m in matches), default=1)


def _parse_listing_page(html: str) -> list[dict]:
    """Extract URL + basic fields from JSON-LD on a listing results page."""
    blocks = re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    items = []
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "ItemList":
            continue
        for entry in data.get("itemListElement", []):
            item = entry.get("item", {})
            offers = item.get("offers", {})
            offered = offers.get("itemOffered", {})
            additional = {
                p["name"]: p["value"]
                for p in item.get("additionalProperty", [])
            }
            currency = offers.get("priceCurrency")
            price = offers.get("price")
            if currency and currency.upper() not in ("USD", "U$S"):
                price = None  # no mezclar pesos con dólares
            items.append(
                {
                    "url": item.get("url"),
                    "name": item.get("name"),
                    "description": (item.get("description") or "").replace(
                        "\r\n", "\n"
                    ),
                    "price_usd": price,
                    "m2_total": additional.get("Sup.Tot."),
                    "m2_cubiertos": additional.get("Sup. Cub."),
                    "dormitorios": additional.get("Dorm."),
                    "banios": additional.get("Baño"),
                    "ambientes": offered.get("numberOfRooms"),
                }
            )
    return items


def _parse_detail_page(html: str) -> dict:
    """Extract full property data from __NEXT_DATA__ on a detail page."""
    nd = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not nd:
        return {}
    try:
        data = json.loads(nd.group(1))
        pd = data["props"]["pageProps"]["propertyData"]
    except (json.JSONDecodeError, KeyError):
        return {}

    attrs = {a["code"]: a.get("value") for a in pd.get("attributes", [])}
    price_obj = pd.get("price") or {}

    return {
        "id": pd.get("id"),
        "address": pd.get("address"),
        "rounded_address": pd.get("rounded_address"),
        "price_usd": price_obj.get("value") if isinstance(price_obj, dict) else None,
        "price_per_meter": pd.get("price_per_meter"),
        "description": pd.get("description") or "",
        "rooms": pd.get("rooms"),
        "bedrooms": pd.get("bedrooms"),
        "restrooms": pd.get("restrooms"),
        # from attributes
        "m2_total": attrs.get("superficie_total"),
        "m2_cubiertos": attrs.get("superficie_cubierta"),
        "m2_descubiertos": attrs.get("superficie_descubierta"),
        "ambientes": attrs.get("cantidad_ambientes"),
        "dormitorios": attrs.get("cantidad_dormitorios"),
        "banios": attrs.get("cantidad_banios"),
        "expensas_ars": attrs.get("expensas"),
        "fecha_expensa": attrs.get("fecha_expensa"),
        "orientacion": attrs.get("orientacion"),
        "luminosidad": attrs.get("luminosidad"),
        "disposicion": attrs.get("disposicion"),
        "estado_inmueble": attrs.get("estado_inmueble"),
        "etapa_inmueble": attrs.get("etapa_inmueble"),
        "antiguedad": attrs.get("antiguedad"),
        "tipo_edificio": attrs.get("tipo_edificio"),
        "estado_edificio": attrs.get("estado_edificio"),
        "pisos_edificio": attrs.get("cantidad_pisos_edificio"),
    }


def scrape_all(delay: float = REQUEST_DELAY) -> list[dict]:
    """
    Full scrape: listing pages → detail pages → merged dicts.
    Returns one dict per property with all available fields.
    """
    session = make_session()

    # ── Phase 1: collect URLs from listing pages ────────────────────────────
    logger.info("Phase 1: collecting listing URLs…")
    html1 = _fetch(session, BASE_URL)
    max_page = _get_max_page(html1)
    logger.info(f"  {max_page} page(s) detected")

    basic_listings = _parse_listing_page(html1)
    seen_urls = {l["url"] for l in basic_listings if l.get("url")}
    time.sleep(delay)

    for page in range(2, max_page + 1):
        url = f"{BASE_URL}/pagina-{page}"
        logger.info(f"  Fetching results page {page}: {url}")
        try:
            html = _fetch(session, url)
        except requests.RequestException as exc:
            logger.warning(f"  Failed to fetch results page {page}: {exc} — stopping pagination")
            break
        new_listings = _parse_listing_page(html)

        # Stop if this page is a duplicate of a previous page
        new_urls = {l["url"] for l in new_listings if l.get("url")}
        if new_urls and new_urls.issubset(seen_urls):
            logger.info(f"  Page {page} is a duplicate — stopping pagination")
            break

        fresh = [l for l in new_listings if l.get("url") not in seen_urls]
        basic_listings.extend(fresh)
        seen_urls.update(new_urls)
        time.sleep(delay)

    logger.info(f"  {len(basic_listings)} unique listings found")

    # ── Phase 2: fetch detail pages ─────────────────────────────────────────
    logger.info("Phase 2: fetching detail pages…")
    merged = []
    for i, listing in enumerate(basic_listings):
        detail_url = listing.get("url")
        if not detail_url:
            listing.update({"piso": extract_piso(listing.get("description")),
                            "cochera": has_cochera(listing.get("description"))})
            merged.append(listing)
            continue

        logger.info(f"  ({i+1}/{len(basic_listings)}) {listing.get('name')} → {detail_url}")
        try:
            html = _fetch(session, detail_url)
            detail = _parse_detail_page(html)

            # Detail values overwrite basic values where present
            result = {**listing}
            for k, v in detail.items():
                if v is not None:
                    result[k] = v

            desc = detail.get("description") or listing.get("description") or ""
            result["piso"] = extract_piso(
                detail.get("address") or listing.get("address"), desc
            )
            result["cochera"] = has_cochera(desc)
        except requests.RequestException as exc:
            logger.warning(f"    ⚠ Failed to fetch detail: {exc}")
            result = {**listing}
            result["piso"] = extract_piso(listing.get("description"))
            result["cochera"] = has_cochera(listing.get("description"))

        merged.append(result)
        time.sleep(delay)

    logger.info(f"Scrape complete — {len(merged)} properties")
    return merged
