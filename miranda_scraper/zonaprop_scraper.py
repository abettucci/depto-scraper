"""
Zonaprop scraper — departamentos en venta en Recoleta, 50-150 m².

Toda la data está en window.__PRELOADED_STATE__ (no se necesitan detail pages).
30 listings por página; limitamos a MAX_PAGES para el run diario.
"""

import json
import logging
import re
import time
from typing import Optional

import requests

from common import (
    MAX_M2, MAX_PRICE_USD, USER_AGENT, extract_amenities, extract_piso, has_cochera,
    make_session, within_rent_cap,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.zonaprop.com.ar/departamentos-venta-recoleta-50-150-m2"
ZP_HOST = "https://www.zonaprop.com.ar"

# Zona norte: San Isidro + el bajo (San Isidro Labrador), con gimnasio (proxy de complejo
# con amenities), 65-200 m² cubiertos. El tope de 150 m² y de USD 400k se aplica en código.
_NORTE_SLUG = (
    "san-isidro-labrador-san-isidro-san-isidro-san-isidro-con-gimnasio-65-200-m2-cubiertos"
)
# Venta: el portal ya filtra a menos de USD 400.000 (se re-chequea en código por las dudas)
NORTE_VENTA_URL = (
    f"https://www.zonaprop.com.ar/departamentos-venta-{_NORTE_SLUG}-menos-{MAX_PRICE_USD}-dolar"
)
# Alquiler: mismo slug cambiando la operación y sin el filtro de precio en USD
# (convención de Zonaprop; sin confirmar)
NORTE_ALQUILER_URL = f"https://www.zonaprop.com.ar/departamentos-alquiler-{_NORTE_SLUG}"
MAX_PAGES = 5         # 5 × 30 = 150 listings/día
REQUEST_DELAY = 2.5   # ZP es sensible al scraping rápido

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",  # sin br: requests no decodifica brotli
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Códigos de orientación de Zonaprop → texto legible
ORIENT_MAP = {
    "N": "Norte", "S": "Sur", "E": "Este", "O": "Oeste",
    "NE": "Noreste", "NO": "Noroeste", "SE": "Sudeste", "SO": "Sudoeste",
}


FALLBACK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4.1 Safari/605.1.15"
)


def _fetch(session: requests.Session, url: str, retry: bool = True) -> str:
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text
    # Si ZP sirvió una página sin estado (bot detection), reintentar con otro UA
    if retry and "window.__PRELOADED_STATE__" not in html:
        time.sleep(3)
        alt_headers = {**HEADERS, "User-Agent": FALLBACK_UA}
        resp2 = session.get(url, headers=alt_headers, timeout=20)
        resp2.raise_for_status()
        return resp2.text
    return html


def _extract_state(html: str) -> Optional[dict]:
    """Extract window.__PRELOADED_STATE__ JSON from page."""
    marker = "window.__PRELOADED_STATE__ = "
    idx = html.find(marker)
    if idx == -1:
        return None
    try:
        # raw_decode respeta llaves dentro de strings (descripciones con { })
        state, _ = json.JSONDecoder().raw_decode(html, idx + len(marker))
    except json.JSONDecodeError as exc:
        logger.warning(f"  __PRELOADED_STATE__ JSON inválido: {exc}")
        return None
    return state if isinstance(state, dict) else None


def _to_float(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _safe_feature(features: dict, key: str) -> Optional[str]:
    """Get feature value by ID from mainFeatures dict."""
    feat = features.get(key)
    if feat and isinstance(feat, dict):
        val = feat.get("value")
        return str(val) if val is not None else None
    return None


def _clean_address(raw: str) -> str:
    """Strip 'Departamentos Venta ' prefix that Zonaprop adds."""
    raw = raw or ""
    for prefix in [
        "Departamentos Venta ",
        "Departamento en Venta ",
        "Desarrollos verticales Venta ",
    ]:
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _parse_posting(p: dict, allow_ars: bool = False) -> dict:
    """Convert a single Zonaprop posting dict to our canonical format.

    allow_ars=True (alquileres) también acepta precios en pesos → price_ars.
    """
    # Price
    price_usd = None
    price_ars = None
    for op in p.get("priceOperationTypes") or []:
        for pr in op.get("prices") or []:
            cur, amt = pr.get("currency"), pr.get("amount")
            if cur in ("USD", "U$S") and price_usd is None:
                price_usd = amt
            elif allow_ars and cur in ("$", "ARS", "PESOS") and price_ars is None:
                price_ars = amt

    # Expensas (currency is "$" = ARS)
    exp = p.get("expenses") or {}
    expensas_ars = None
    if exp.get("currency") in ("$", "ARS", "PESOS") and exp.get("amount"):
        expensas_ars = exp["amount"]

    # Features
    feats = p.get("mainFeatures") or {}
    m2_total = _safe_feature(feats, "CFT100")
    m2_cubiertos = _safe_feature(feats, "CFT101")
    ambientes = _safe_feature(feats, "CFT1")
    dormitorios = _safe_feature(feats, "CFT2")
    banios = _safe_feature(feats, "CFT3")
    antiguedad = _safe_feature(feats, "CFT5")

    # Orientación (code like "N", "NE" → "Norte", "Noreste")
    orient_code = _safe_feature(feats, "1000029")
    orientacion = ORIENT_MAP.get(orient_code, orient_code) if orient_code else None

    # Cochera (CFT4 = Cocheras) + mención en título/descripción
    cocheras = _to_float(_safe_feature(feats, "CFT4"))
    title = p.get("title") or ""
    name = (p.get("generatedTitle") or title or "").strip()
    cochera = bool(cocheras and cocheras > 0) or has_cochera(name)

    # Address
    house = p.get("house") or {}
    raw_addr = (house.get("address") or {}).get("name") or ""
    address = _clean_address(raw_addr)

    # URL
    relative_url = p.get("url") or ""
    full_url = ZP_HOST + relative_url if relative_url.startswith("/") else relative_url

    # Description
    description = p.get("descriptionNormalized") or ""

    return {
        "source": "zonaprop",
        "id": str(p.get("postingId") or ""),
        "url": full_url,
        "name": name,
        "address": address,
        "rounded_address": address,
        "description": description,
        "price_usd": _to_float(price_usd),
        "price_ars": _to_float(price_ars),
        "amenities": extract_amenities(name + " " + description),
        "m2_total": _to_float(m2_total),
        "m2_cubiertos": _to_float(m2_cubiertos) or _to_float(m2_total),
        "ambientes": _to_float(ambientes),
        "dormitorios": _to_float(dormitorios),
        "banios": _to_float(banios),
        "antiguedad": _to_float(antiguedad),
        "orientacion": orientacion,
        "cochera": cochera,
        "expensas_ars": _to_float(expensas_ars),
        # Not available in listing page — extract from description
        "piso": extract_piso(description),
        "etapa_inmueble": None,
        "estado_inmueble": None,
    }


def _scrape_zp(
    base_url: str,
    label: str,
    max_pages: int,
    delay: float,
    allow_ars: bool = False,
    max_m2: Optional[float] = None,
) -> list[dict]:
    """Pagina una búsqueda de Zonaprop (base_url sin .html) y devuelve listings canónicos."""
    session = make_session()
    all_listings = []
    seen_ids: set[str] = set()
    skipped = {"desarrollo": 0, "sin_precio_o_m2": 0, "sobre_tope": 0, "sobre_m2": 0}
    total_pages = 1
    page = 0

    for page in range(1, max_pages + 1):
        url = base_url + ".html" if page == 1 else f"{base_url}-pagina-{page}.html"
        logger.info(f"  {label} page {page}/{max_pages}: {url}")

        try:
            html = _fetch(session, url)
        except requests.RequestException as exc:
            logger.warning(f"  Failed to fetch {url}: {exc}")
            break

        state = _extract_state(html)
        if not state:
            logger.warning(
                f"  No __PRELOADED_STATE__ on page {page} "
                f"(¿bloqueo anti-bot? respuesta de {len(html)} chars)"
            )
            break

        postings = state.get("listStore", {}).get("listPostings") or []
        if not postings:
            logger.info(f"  No postings on page {page}, stopping")
            break

        paging = state.get("listStore", {}).get("paging") or {}
        total_pages = paging.get("totalPages", 1)

        new_count = 0
        for p in postings:
            pid = str(p.get("postingId") or "")
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            # Skip development projects (no individual unit data)
            title = (p.get("generatedTitle") or p.get("title") or "").lower()
            if "desarrollo vertical" in title or "emprendimiento" in title:
                skipped["desarrollo"] += 1
                continue

            parsed = _parse_posting(p, allow_ars=allow_ars)
            # Skip if no price or no m² (can't run price model)
            if not ((parsed.get("price_usd") or parsed.get("price_ars")) and parsed.get("m2_cubiertos")):
                skipped["sin_precio_o_m2"] += 1
                continue
            # Tope de venta en USD; en alquiler el tope (si hay) se aplica aparte
            if not allow_ars and parsed["price_usd"] > MAX_PRICE_USD:
                skipped["sobre_tope"] += 1
                continue
            if max_m2 is not None and parsed["m2_cubiertos"] > max_m2:
                skipped["sobre_m2"] += 1
                continue
            all_listings.append(parsed)
            new_count += 1

        logger.info(f"  Page {page}: {new_count} new listings (total so far: {len(all_listings)})")

        if page >= total_pages:
            logger.info("  Reached last page")
            break

        time.sleep(delay)

    if total_pages > page:
        logger.warning(
            f"  {label}: hay {total_pages} páginas pero solo se leyeron {page} "
            f"(MAX_PAGES={max_pages}) — resultados truncados"
        )
    logger.info(f"{label} descartados: {skipped}")
    logger.info(f"{label} scrape complete: {len(all_listings)} listings")
    return all_listings


def scrape_zonaprop(max_pages: int = MAX_PAGES, delay: float = REQUEST_DELAY) -> list[dict]:
    """Scrape Zonaprop Recoleta (venta). Returns list of canonical listing dicts."""
    return _scrape_zp(BASE_URL, "Zonaprop", max_pages, delay)


def scrape_zonaprop_norte(max_pages: int = MAX_PAGES, delay: float = REQUEST_DELAY) -> list[dict]:
    """
    Scrape Zonaprop zona norte (San Isidro + el bajo), complejos con gimnasio.
    Devuelve venta y alquiler juntos; cada listing trae "operation" ("venta"/"alquiler").
    """
    results: list[dict] = []
    for operation, base_url in (("venta", NORTE_VENTA_URL), ("alquiler", NORTE_ALQUILER_URL)):
        is_rent = operation == "alquiler"
        batch = _scrape_zp(
            base_url, f"Zonaprop norte {operation}", max_pages, delay,
            allow_ars=is_rent, max_m2=MAX_M2,
        )
        if is_rent:
            batch = [l for l in batch if within_rent_cap(l)]
        for l in batch:
            l["segment"] = "norte"
            l["operation"] = operation
        results.extend(batch)
        time.sleep(delay)
    return results
