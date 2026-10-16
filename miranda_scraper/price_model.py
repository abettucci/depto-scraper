"""
Modelo de precios para departamentos en Recoleta.

Fuente de referencia: USD/m² 2024-2025 por zona.
"""

import logging
import re
from typing import Optional

import requests

from common import MAX_PRICE_USD, MIN_M2 as MIN_M2_CUBIERTOS

logger = logging.getLogger(__name__)

# ── Zonas y precios base ────────────────────────────────────────────────────

ZONES = {
    # Recoleta Centro: caja delimitada por Libertador · Santa Fe · Cerrito · Pueyrredón
    "centro": {
        "streets": [
            # eje E-O (dentro de la caja)
            "libertador", "posadas", "guido", "juncal", "arroyo",
            "alvear", "marcelo t", "marcelo torcuato",
            "quintana", "arenales", "santa fe",
            # eje N-S (dentro de la caja)
            "cerrito", "libertad", "esmeralda", "suipacha",
            "uruguay", "talcahuano", "montevideo",
            "rodriguez pena", "rodriguez peña",
            "junin", "junín", "ayacucho",
            "callao", "riobamba", "uriburu",
            "pueyrredon", "pueyrredón",
        ],
        "base_usd_m2": 2900,
    },
    # Recoleta Media: al norte de Santa Fe y bordes (dentro del barrio)
    "media": {
        "streets": [
            "paraguay", "charcas", "mansilla", "las heras",
            "french", "peña", "pena", "pacheco de melo", "beruti",
        ],
        "base_usd_m2": 2400,
    },
    # Recoleta Exterior: al este de Pueyrredón y límites exteriores del barrio
    "exterior": {
        "streets": [
            "bulnes", "austria", "agüero", "aguero",
            "laprida", "billinghurst", "anchorena", "larrea",
            "coronel diaz",
            "cordoba", "córdoba",
            "sanchez de bustamante", "sánchez de bustamante",
        ],
        "base_usd_m2": 1900,
    },
}

ZONE_LABELS = {
    "centro": "Recoleta Centro",
    "media": "Recoleta Media",
    "exterior": "Recoleta Exterior",
}

FALLBACK_RATE_ARS_PER_USD = 1200.0


# ── Tipo de cambio ──────────────────────────────────────────────────────────

def get_usd_ars_rate() -> float:
    """Fetch live USD/ARS from open.er-api.com, fall back to 1200 on error."""
    try:
        resp = requests.get(
            "https://open.er-api.com/v6/latest/USD", timeout=5
        )
        resp.raise_for_status()
        data = resp.json()
        rate = data["rates"].get("ARS")
        if rate and rate > 0:
            logger.info(f"Live USD/ARS rate: {rate:.0f}")
            return float(rate)
    except Exception as exc:
        logger.warning(f"Could not fetch exchange rate: {exc} — using {FALLBACK_RATE_ARS_PER_USD}")
    return FALLBACK_RATE_ARS_PER_USD


# ── Helpers ─────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lower-case + strip common Spanish accents for robust matching."""
    return (
        text.lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
        .replace("ü", "u")
    )


def detect_zone(address: Optional[str], rounded_address: Optional[str]) -> tuple[str, int]:
    """Return (zone_key, base_usd_m2) for a property address."""
    combined = _normalize(f"{address or ''} {rounded_address or ''}")
    for zone_key, zone_data in ZONES.items():
        for street in zone_data["streets"]:
            if _normalize(street) in combined:
                return zone_key, zone_data["base_usd_m2"]
    # Default: media (most common Recoleta zone)
    logger.debug(f"Zone not detected for '{address}', defaulting to 'media'")
    return "media", ZONES["media"]["base_usd_m2"]


def detect_estado(
    etapa_inmueble: Optional[str],
    estado_inmueble: Optional[str],
    description: Optional[str],
) -> str:
    """
    Returns one of: 'a_estrenar', 'reciclado', 'a_reciclar', 'normal'.

    Priority: etapa_inmueble > description keywords > estado_inmueble
    """
    etapa = _normalize(etapa_inmueble or "")
    if "a estrenar" in etapa or "estrenar" in etapa:
        return "a_estrenar"
    if "a reciclar" in etapa or "reciclar" in etapa:
        return "a_reciclar"
    if "en construccion" in etapa or "construccion" in etapa:
        return "a_estrenar"

    desc = _normalize(description or "")
    if re.search(r"a reciclar|necesita refacci|para refaccionar|a demoler", desc):
        return "a_reciclar"
    if re.search(r"\ba estrenar\b|a nuevo", desc):
        return "a_estrenar"
    if re.search(
        r"reciclad[oa]|refaccionad[oa]|totalmente renovad[oa]|hecho a nuevo"
        r"|completamente renovad[oa]|remodelad[oa]",
        desc,
    ):
        return "reciclado"

    estado = _normalize(estado_inmueble or "")
    if "reciclar" in estado or "demolicion" in estado:
        return "a_reciclar"

    return "normal"


def detect_orientacion_norte(orientacion: Optional[str]) -> bool:
    """True if orientation contains north component."""
    if not orientacion:
        return False
    norm = _normalize(orientacion)
    return "norte" in norm or "noreste" in norm or "noroeste" in norm


# ── Modelo principal ─────────────────────────────────────────────────────────

def analyze_listing(
    listing: dict,
    usd_ars_rate: float,
    zone_stats: Optional[dict] = None,
) -> dict:
    """
    Add 'analysis' key to listing dict with price evaluation.
    Returns listing unchanged (with analysis=None) if price or m² missing.

    zone_stats: output of data_logger.load_zone_stats(). When provided and
    there's enough data for the listing's zone, the model uses historical
    medians/percentiles instead of fixed reference prices.
    """
    price_usd = listing.get("price_usd")
    m2_cub = listing.get("m2_cubiertos")

    if not price_usd or not m2_cub:
        return {**listing, "analysis": None}

    try:
        price_usd = float(price_usd)
        m2_cub = float(m2_cub)
    except (TypeError, ValueError):
        return {**listing, "analysis": None}

    # ── Zone ────────────────────────────────────────────────────────────────
    zone_key, base_price = detect_zone(
        listing.get("address"), listing.get("rounded_address")
    )

    # ── Estado ──────────────────────────────────────────────────────────────
    estado = detect_estado(
        listing.get("etapa_inmueble"),
        listing.get("estado_inmueble"),
        listing.get("description"),
    )

    # ── Multiplicadores ──────────────────────────────────────────────────────
    multiplier = 1.0
    adjustments = []

    # Orientación norte
    if detect_orientacion_norte(listing.get("orientacion")):
        multiplier += 0.10
        adjustments.append("+10% norte")

    # Estado
    if estado == "a_estrenar":
        multiplier += 0.12
        adjustments.append("+12% a estrenar")
    elif estado == "reciclado":
        multiplier += 0.12
        adjustments.append("+12% reciclado")
    elif estado == "a_reciclar":
        multiplier -= 0.15
        adjustments.append("-15% a reciclar")

    # Piso 5+
    piso = listing.get("piso")
    if piso is not None:
        try:
            if int(piso) >= 5:
                multiplier += 0.08
                adjustments.append(f"+8% piso {piso}")
        except (TypeError, ValueError):
            pass

    # Superficie descubierta (balcón/terraza)
    m2_total = listing.get("m2_total")
    outdoor_m2 = 0.0
    cov_ratio = None
    if m2_total:
        try:
            m2_total = float(m2_total)
            if m2_total > 0:
                outdoor_m2 = max(0.0, m2_total - m2_cub)
                cov_ratio = m2_cub / m2_total
                if outdoor_m2 >= 8:
                    multiplier += 0.05
                    adjustments.append(f"+5% balcón/terraza ({outdoor_m2:.0f} m² desc.)")
        except (TypeError, ValueError):
            pass

    # Cochera (+25,000 USD flat)
    cochera_bonus = 25_000 if listing.get("cochera") else 0
    if cochera_bonus:
        adjustments.append("+USD 25k cochera")

    # Expensas altas (> ARS 200k/mes → costo capitalizado 60 meses)
    expensas_cost_usd = 0.0
    expensas_ars = listing.get("expensas_ars")
    if expensas_ars:
        try:
            exp = float(expensas_ars)
            if exp > 200_000 and usd_ars_rate > 0:
                expensas_cost_usd = (exp / usd_ars_rate) * 60
                adjustments.append(f"-USD {expensas_cost_usd:,.0f} expensas altas")
        except (TypeError, ValueError):
            pass

    # ── Reference price ──────────────────────────────────────────────────────
    # Use historical median if available (passed in via zone_stats), else fixed base
    hist = (zone_stats or {}).get(zone_key)
    using_historical = False
    if hist:
        base_price = hist["median"]
        using_historical = True
        adjustments.append(f"(base histórica: {base_price:.0f} USD/m², n={hist['count']})")

    ref_price_m2_adjusted = base_price * multiplier
    ref_total = ref_price_m2_adjusted * m2_cub + cochera_bonus - expensas_cost_usd
    ref_total = max(ref_total, 1)

    # ── Actual price metrics ─────────────────────────────────────────────────
    actual_price_m2 = price_usd / m2_cub

    # Reference price/m² for display (proportional from ref_total)
    ref_price_m2_display = ref_total / m2_cub

    delta_pct = ((price_usd - ref_total) / ref_total) * 100

    # ── Evaluation base ──────────────────────────────────────────────────────
    # When historical data is available, use P25/P75 for zone thresholds.
    if hist and using_historical:
        if actual_price_m2 < hist["p25"]:
            evaluation = "green"
        elif actual_price_m2 > hist["p75"]:
            evaluation = "red"
        else:
            evaluation = "yellow"
    else:
        if delta_pct < -20:
            evaluation = "green"
        elif delta_pct > 12:
            evaluation = "red"
        else:
            evaluation = "yellow"

    # ── 🔥 Oportunidad real — tier estricto ──────────────────────────────────
    # Requiere: 3+ de 5 señales Y precio ≥35% bajo referencia.
    # Señal 2 (zona) es casi siempre true → necesitamos señal 1 + zona + 1 más.
    fire_signals = 0
    # 1. Descuento muy fuerte (>35% bajo referencia ajustada)
    if delta_pct < -35:
        fire_signals += 1
    # 2. Zona buena (centro o media)
    if zone_key in ("centro", "media"):
        fire_signals += 1
    # 3. Orientación norte
    if detect_orientacion_norte(listing.get("orientacion")):
        fire_signals += 1
    # 4. Piso 5+
    piso_val = listing.get("piso")
    if piso_val is not None:
        try:
            if int(piso_val) >= 5:
                fire_signals += 1
        except (TypeError, ValueError):
            pass
    # 5. Balcón/terraza significativa (>= 8 m² descubiertos)
    if outdoor_m2 >= 8:
        fire_signals += 1

    # "Oportunidad real": 4+ señales Y precio al menos 35% bajo referencia
    # Con zona (señal 2) casi siempre true y delta<-35 (señal 1) requerido,
    # necesita 2 atributos adicionales: norte, piso5+, o outdoor≥8m²
    is_fire = fire_signals >= 4 and delta_pct < -35

    if is_fire:
        evaluation = "fire"
        emoji = "🔥"
    elif evaluation == "green":
        emoji = "🟢"
    elif evaluation == "red":
        emoji = "🔴"
    else:
        emoji = "🟡"

    # Legacy flag (a_reciclar con buen precio = señalarlo igual)
    opportunity = estado == "a_reciclar" and evaluation in ("green", "yellow", "fire")

    # ── Composite opportunity score (para ordenar) ────────────────────────────
    # Más alto = mejor oportunidad. Usado como clave de sort.
    zone_bonus = {"centro": 20, "media": 10, "exterior": 0}
    try:
        piso_bonus = 6 if (piso_val is not None and int(piso_val) >= 5) else 0
    except (TypeError, ValueError):
        piso_bonus = 0

    # USD/m² bajo es la métrica principal — normalizamos contra el ref de la zona
    # Un 10% más barato que la ref vale ~10 puntos; escala linealmente
    price_m2_discount = max(0, -delta_pct)  # positivo cuando es más barato

    score = (
        price_m2_discount * 1.5                                 # USD/m² bajo = principal
        + zone_bonus.get(zone_key, 0)                          # calidad de zona
        + (8 if detect_orientacion_norte(listing.get("orientacion")) else 0)
        + piso_bonus
        + (5 if listing.get("cochera") else 0)
        + (4 if estado == "a_estrenar" else 3 if estado == "reciclado" else 0)
        + min(outdoor_m2 / 4, 8)                               # hasta +8 pts por outdoor
        + fire_signals * 2                                      # señales de oportunidad
        - (20 if estado == "a_reciclar" else 0)                # descuenta costo oculto
        - (5 if (expensas_cost_usd or 0) > 20_000 else 0)      # expensas muy altas
    )

    return {
        **listing,
        "estado_detected": estado,
        "analysis": {
            "zone": zone_key,
            "zone_label": ZONE_LABELS[zone_key],
            "base_price_m2": base_price,
            "multiplier": round(multiplier, 3),
            "adjustments": adjustments,
            "cochera_bonus": cochera_bonus,
            "expensas_cost_usd": round(expensas_cost_usd),
            "ref_total": round(ref_total),
            "ref_price_m2": round(ref_price_m2_display),
            "actual_price_m2": round(actual_price_m2),
            "delta_pct": round(delta_pct, 1),
            "evaluation": evaluation,
            "emoji": emoji,
            "fire_signals": fire_signals,
            "score": round(score, 1),
            "opportunity": opportunity,
        },
    }


# MIN_M2_CUBIERTOS y MAX_PRICE_USD vienen de common.py (compartidos con los scrapers)


def analyze_all(
    listings: list[dict],
    usd_ars_rate: Optional[float] = None,
    zone_stats: Optional[dict] = None,
) -> list[dict]:
    """Analyze price for all listings and sort by opportunity.

    zone_stats: pass in data_logger.load_zone_stats() for self-improving model.
    """
    if usd_ars_rate is None:
        usd_ars_rate = get_usd_ars_rate()

    # Filtrar por superficie mínima y precio máximo
    filtered = []
    for l in listings:
        m2    = l.get("m2_cubiertos")
        price = l.get("price_usd")
        try:
            if m2 is not None and float(m2) < MIN_M2_CUBIERTOS:
                logger.debug(f"Skipping {l.get('name') or l.get('address')} — m2_cub={m2} < {MIN_M2_CUBIERTOS}")
                continue
        except (TypeError, ValueError):
            pass
        try:
            if price is not None and float(price) > MAX_PRICE_USD:
                logger.debug(f"Skipping {l.get('name') or l.get('address')} — price={price} > {MAX_PRICE_USD}")
                continue
        except (TypeError, ValueError):
            pass
        filtered.append(l)

    analyzed = [analyze_listing(l, usd_ars_rate, zone_stats) for l in filtered]

    # Sort by tier, then source (MB first), then by composite score descending.
    tier_order = {"fire": 0, "green": 1, "yellow": 2, "red": 3, None: 4}
    source_order = {"miranda_bosch": 0, "zonaprop": 1}

    def sort_key(l: dict):
        a = l.get("analysis") or {}
        return (
            tier_order.get(a.get("evaluation"), 4),
            source_order.get(l.get("source"), 2),
            -(a.get("score") or 0),
        )

    return sorted(analyzed, key=sort_key)
