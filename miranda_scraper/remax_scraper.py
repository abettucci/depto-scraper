"""
RE/MAX Argentina scraper.

  - scrape_remax():       departamentos en venta en Recoleta, 65-150 m², hasta USD 400k.
  - scrape_remax_norte(): complejos con amenities (filtro gimnasio) en San Isidro y el bajo,
                          venta y alquiler, 65-150 m².

Usa la API REST interna: api-ar.redremax.com (no requiere autenticación).
24 listings por página; hasta MAX_PAGES páginas por corrida.
"""

import logging
import re
import time
from typing import Optional

import requests

from common import (
    MAX_M2, MAX_PRICE_USD, MIN_M2, USER_AGENT, extract_amenities, extract_piso,
    has_cochera, make_session, within_rent_cap,
)

logger = logging.getLogger(__name__)

API_URL = "https://api-ar.redremax.com/remaxweb-ar/api/listings/findAllWithEntrepreneurships"
REMAX_HOST = "https://www.remax.com.ar"
MAX_PAGES = 5        # 5 × 24 = 120 listings/día
REQUEST_DELAY = 0.8

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "es-AR,es;q=0.9",
    "Referer": "https://www.remax.com.ar/",
}

BASE_PARAMS = {
    "sort": "-createdAt",
    "in": ["operationId:1", "typeId:1,2,3,4,5,6,7,8"],
    "pricein": f"1:0:{MAX_PRICE_USD}",
    "gte": f"dimensionCovered:{MIN_M2}",
    "lte": f"dimensionCovered:{MAX_M2}",
    "locations": "in::::25033@<b>Recoleta</b>:::",
    "pageSize": 24,
}

# Zona norte: San Isidro (116) + San Isidro Labrador (2236, "el bajo").
# featuresin=13 es el filtro "gimnasio", que usamos como proxy de complejo con amenities.
NORTE_LOCATIONS = "in:::116@San Isidro:::2236@San Isidro Labrador:"
NORTE_FEATURE_GYM = "13"
NORTE_OPERATIONS = {"venta": 1, "alquiler": 2}


def _norte_params(operation_id: int) -> dict:
    params = {
        "sort": "-createdAt",
        "in": [f"operationId:{operation_id}", "typeId:1,2,3,4,5,6,7,8"],
        "featuresin": NORTE_FEATURE_GYM,
        "gte": f"dimensionCovered:{MIN_M2}",
        "lte": f"dimensionCovered:{MAX_M2}",
        "locations": NORTE_LOCATIONS,
        "pageSize": 24,
    }
    if operation_id == NORTE_OPERATIONS["venta"]:
        params["pricein"] = f"1:0:{MAX_PRICE_USD}"
    # alquiler: sin pricein para no excluir avisos en pesos; el tope se aplica en código
    return params


# Orientación solo con contexto explícito ("orientación norte", "orientado al sur").
# Buscar "este"/"norte" sueltos da falsos positivos ("este departamento", "Barrio Norte").
_ORIENT = r"(noreste|noroeste|sudeste|sudoeste|norte|sur|este|oeste)"
_ORIENT_RE = re.compile(
    rf"orientaci[oó]n\s+(?:al\s+|hacia\s+el\s+)?{_ORIENT}\b"
    rf"|orientad[oa]s?\s+(?:al\s+|hacia\s+el\s+)?{_ORIENT}\b",
    re.IGNORECASE,
)


def _extract_orientacion(text: str) -> Optional[str]:
    m = _ORIENT_RE.search(text or "")
    if not m:
        return None
    return (m.group(1) or m.group(2)).capitalize()


def _parse_listing(item: dict, allow_ars: bool = False) -> Optional[dict]:
    """Convierte un item de la API RE/MAX al formato canónico.

    Por defecto solo acepta precios en USD (modelo de Recoleta). Con allow_ars=True
    acepta también pesos (alquileres): queda en price_ars y price_usd es None.
    """
    price_usd = None
    price_ars = None
    currency = (item.get("currency") or {}).get("value", "")
    raw_price = item.get("price")
    if raw_price:
        try:
            amount = float(raw_price)
        except (TypeError, ValueError):
            amount = None
        if amount:
            if currency == "USD":
                price_usd = amount
            elif allow_ars and currency in ("ARS", "$", "PESOS"):
                price_ars = amount

    if not price_usd and not price_ars:
        return None

    # Superficie
    m2_cub = item.get("dimensionCovered")
    m2_tot = item.get("dimensionTotalBuilt")
    try:
        m2_cub = float(m2_cub) if m2_cub else None
        m2_tot = float(m2_tot) if m2_tot else None
    except (TypeError, ValueError):
        m2_cub = m2_tot = None

    # Expensas
    expensas_ars = None
    exp_currency = (item.get("expensesCurrency") or {}).get("value", "")
    if exp_currency in ("ARS", "$", "PESOS") and item.get("expensesPrice"):
        try:
            exp_val = float(item["expensesPrice"])
            if exp_val >= 1000:   # ignorar valores basura como $1
                expensas_ars = exp_val
        except (TypeError, ValueError):
            pass

    # Ambientes / dormitorios
    ambientes  = item.get("totalRooms")
    dormitorios = item.get("bedrooms")

    # Dirección
    address = item.get("displayAddress") or item.get("geoLabel") or ""

    # URL — el slug del API puede ser ruta completa o solo el código RE-RXXXXX
    raw_url  = item.get("url") or ""
    slug     = item.get("slug") or ""
    lid      = str(item.get("listingId") or "")
    if raw_url:
        url = raw_url if raw_url.startswith("http") else f"{REMAX_HOST}/{raw_url.lstrip('/')}"
        # El API devuelve /listings/buy/ pero el sitio usa /listings/
        url = url.replace("/listings/buy/", "/listings/").replace("/listings/rent/", "/listings/")
    elif slug:
        url = slug if slug.startswith("http") else f"{REMAX_HOST}/listings/{slug.lstrip('/')}"
    elif lid:
        url = f"{REMAX_HOST}/listings/{lid}"
    else:
        url = ""

    # Título y descripción (para extraer piso y orientación)
    title = item.get("title") or ""
    description = item.get("description") or ""

    # Piso (del título/descripción, RE/MAX no lo expone como campo)
    piso = extract_piso(title, description)

    # Orientación — RE/MAX no la expone en el listado; solo con contexto explícito
    orientacion = _extract_orientacion(title + " " + description)

    # Cochera
    cochera = False
    garage = item.get("parkings") or item.get("garages") or 0
    try:
        cochera = int(garage) > 0
    except (TypeError, ValueError):
        pass
    if not cochera:
        cochera = has_cochera(title, description)

    return {
        "source": "remax",
        "id": str(item.get("listingId") or item.get("id") or ""),
        "url": url,
        "name": title or address,
        "address": address,
        "rounded_address": address,
        "description": description,
        "price_usd": price_usd,
        "price_ars": price_ars,
        "amenities": extract_amenities(title + " " + description),
        "m2_total": m2_tot,
        "m2_cubiertos": m2_cub,
        "ambientes": float(ambientes) if ambientes else None,
        "dormitorios": float(dormitorios) if dormitorios else None,
        "banios": float(item.get("bathrooms")) if item.get("bathrooms") else None,
        "antiguedad": None,
        "orientacion": orientacion,
        "cochera": cochera,
        "expensas_ars": expensas_ars,
        "piso": piso,
        "etapa_inmueble": None,
        "estado_inmueble": None,
    }


def _scrape(
    params: dict,
    label: str,
    max_pages: int,
    delay: float,
    allow_ars: bool = False,
) -> list[dict]:
    """Pagina la API de RE/MAX con `params` y devuelve listings canónicos."""
    session = make_session()
    all_listings = []
    seen_ids: set[str] = set()
    total_pages = 1
    page = 0

    for page in range(max_pages):
        logger.info(f"  {label} page {page + 1}/{max_pages}")

        try:
            resp = session.get(API_URL, params={**params, "page": page}, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning(f"  {label} page {page} failed: {exc}")
            break

        inner = data.get("data") or {}
        items = inner.get("data") or []
        if not items:
            logger.info(f"  No items on page {page}, stopping")
            break

        total_pages = inner.get("totalPages", 1)
        new_count = 0

        for item in items:
            lid = str(item.get("listingId") or item.get("id") or "")
            if lid and lid in seen_ids:
                continue
            if lid:
                seen_ids.add(lid)

            parsed = _parse_listing(item, allow_ars=allow_ars)
            if parsed and (parsed.get("price_usd") or parsed.get("price_ars")) and parsed.get("m2_cubiertos"):
                all_listings.append(parsed)
                new_count += 1

        logger.info(f"  Page {page + 1}: {new_count} new listings (total: {len(all_listings)})")

        if page + 1 >= total_pages:
            logger.info(f"  Reached last {label} page")
            break

        time.sleep(delay)

    if total_pages > page + 1:
        logger.warning(
            f"  {label}: hay {total_pages} páginas pero solo se leyeron {page + 1} "
            f"(MAX_PAGES={max_pages}) — resultados truncados"
        )
    logger.info(f"{label} scrape complete: {len(all_listings)} listings")
    return all_listings


def scrape_remax(max_pages: int = MAX_PAGES, delay: float = REQUEST_DELAY) -> list[dict]:
    """Scrape RE/MAX Recoleta (venta). Returns list of canonical listing dicts."""
    return _scrape(BASE_PARAMS, "RE/MAX", max_pages, delay)


def scrape_remax_norte(max_pages: int = MAX_PAGES, delay: float = REQUEST_DELAY) -> list[dict]:
    """
    Scrape RE/MAX zona norte (San Isidro + el bajo), complejos con gimnasio.
    Devuelve venta y alquiler juntos; cada listing trae "operation" ("venta"/"alquiler").
    """
    results: list[dict] = []
    for operation, op_id in NORTE_OPERATIONS.items():
        is_rent = operation == "alquiler"
        batch = _scrape(
            _norte_params(op_id), f"RE/MAX norte {operation}", max_pages, delay, allow_ars=is_rent
        )
        if is_rent:
            batch = [l for l in batch if within_rent_cap(l)]
        for l in batch:
            l["segment"] = "norte"
            l["operation"] = operation
        results.extend(batch)
        time.sleep(delay)
    return results
