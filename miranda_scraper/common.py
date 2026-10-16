"""
Utilidades compartidas por los scrapers (sesión HTTP, piso, cochera).
"""

import re
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Topes de precio/superficie compartidos por las tres fuentes y por price_model.
# MIN_M2 es de superficie cubierta: por debajo, price_model descarta el aviso igual.
MAX_PRICE_USD = 400_000
MIN_M2 = 65
MAX_M2 = 150

# Tope de alquiler mensual en la zona norte. None = sin tope.
RENT_MAX_USD = None
RENT_MAX_ARS = None


def make_session() -> requests.Session:
    """Session con reintentos y backoff ante errores transitorios (429/5xx)."""
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_PISO_PATTERNS = (
    re.compile(r"\bpiso\s+(\d+)", re.IGNORECASE),
    re.compile(r"(\d+)\s*[°ºo]\s*piso", re.IGNORECASE),
)
_PLANTA_BAJA = re.compile(r"planta\s+baja", re.IGNORECASE)


def extract_piso(*texts: Optional[str]) -> Optional[int]:
    """Extrae el número de piso del primer texto donde aparezca."""
    for text in texts:
        if not text:
            continue
        for pat in _PISO_PATTERNS:
            m = pat.search(text)
            if m:
                return int(m.group(1))
        if _PLANTA_BAJA.search(text):
            return 0
    return None


_COCHERA_NEGATIVE = re.compile(
    r"sin cochera"
    r"|no (tiene |incluye |posee )?cochera"
    r"|cochera (opcional|no incluida)"
    r"|opcional (de )?cochera"
    r"|posibilidad de (adquirir |comprar |alquilar )?cochera"
    r"|cochera (a parte|aparte)"
)


def has_cochera(*texts: Optional[str]) -> bool:
    """True si algún texto menciona una cochera incluida (ignora negaciones/opcionales)."""
    for text in texts:
        if not text:
            continue
        low = text.lower()
        if "cochera" in low and not _COCHERA_NEGATIVE.search(low):
            return True
    return False


# Amenities que mostramos en la sección de zona norte (detectadas en el texto del aviso)
_AMENITIES = (
    ("pileta", r"pileta|piscina"),
    ("gimnasio", r"gimnasio|gym"),
    ("SUM", r"\bsum\b|sal[oó]n de usos m[uú]ltiples"),
    ("parrilla", r"parrilla|quincho"),
    ("solárium", r"sol[aá]rium"),
    ("sauna", r"sauna|spa\b"),
    ("seguridad", r"seguridad|vigilancia"),
    ("laundry", r"laundry|lavander[ií]a"),
    ("cancha", r"cancha|paddle|tenis"),
)


def extract_amenities(text: Optional[str]) -> list[str]:
    low = (text or "").lower()
    return [name for name, pat in _AMENITIES if re.search(pat, low)]


def within_rent_cap(listing: dict) -> bool:
    """Aplica RENT_MAX_USD / RENT_MAX_ARS según la moneda del aviso (None = sin tope)."""
    if listing.get("price_usd") and RENT_MAX_USD is not None:
        return listing["price_usd"] <= RENT_MAX_USD
    if listing.get("price_ars") and RENT_MAX_ARS is not None:
        return listing["price_ars"] <= RENT_MAX_ARS
    return True
