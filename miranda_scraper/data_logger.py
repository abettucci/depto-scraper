"""
Persiste el historial diario de listings en Excel para retroalimentar el modelo.

Columnas guardadas por cada listing:
  date, url, id, name, address, zone, m2_cubiertos, m2_total, ambientes,
  dormitorios, piso, orientacion, estado_detected, cochera, expensas_ars,
  price_usd, price_usd_m2, evaluation, delta_pct

El modelo de precio lee este historial para calcular:
  - Mediana USD/m² por zona (cuando hay >= MIN_SAMPLES_FOR_STATS muestras)
  - P25 y P75 por zona para clasificar 🟢/🟡/🔴
"""

import logging
import statistics
from datetime import date
from pathlib import Path
from typing import Optional

import openpyxl
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
HISTORY_FILE = DATA_DIR / "history.xlsx"
SHEET_NAME = "listings"

NORTE_SHEET = "norte"
NORTE_COLUMNS = [
    "date", "url", "property_id", "operation", "name", "address",
    "m2_cubiertos", "ambientes", "dormitorios", "currency", "price",
    "price_m2", "expensas_ars", "amenities",
]

MIN_SAMPLES_FOR_STATS = 8  # minimum observations per zone to use historical model

COLUMNS = [
    "date",
    "url",
    "property_id",
    "name",
    "address",
    "zone",
    "m2_cubiertos",
    "m2_total",
    "ambientes",
    "dormitorios",
    "piso",
    "orientacion",
    "estado_detected",
    "cochera",
    "expensas_ars",
    "price_usd",
    "price_usd_m2",
    "evaluation",
    "delta_pct",
]


def _ensure_workbook() -> openpyxl.Workbook:
    """Load or create the history workbook."""
    DATA_DIR.mkdir(exist_ok=True)
    if HISTORY_FILE.exists():
        wb = openpyxl.load_workbook(HISTORY_FILE)
        if SHEET_NAME not in wb.sheetnames:
            ws = wb.create_sheet(SHEET_NAME)
            ws.append(COLUMNS)
        return wb

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(COLUMNS)
    # Bold headers
    for col_idx, _ in enumerate(COLUMNS, 1):
        ws.cell(row=1, column=col_idx).font = openpyxl.styles.Font(bold=True)
    return wb


def append_listings(analyzed_listings: list[dict], run_date: Optional[date] = None) -> None:
    """Append today's listings to the history Excel. Skips listings without price."""
    today = run_date or date.today()
    wb = _ensure_workbook()
    ws = wb[SHEET_NAME]

    added = 0
    for listing in analyzed_listings:
        price_usd = listing.get("price_usd")
        m2_cub = listing.get("m2_cubiertos")
        if not price_usd or not m2_cub:
            continue

        try:
            price_m2 = float(price_usd) / float(m2_cub)
        except (TypeError, ValueError):
            continue

        a = listing.get("analysis") or {}

        row = [
            today.isoformat(),
            listing.get("url"),
            listing.get("id"),
            listing.get("name"),
            listing.get("address"),
            a.get("zone"),
            _safe_float(m2_cub),
            _safe_float(listing.get("m2_total")),
            _safe_float(listing.get("ambientes")),
            _safe_float(listing.get("dormitorios")),
            listing.get("piso"),
            listing.get("orientacion"),
            listing.get("estado_detected"),
            bool(listing.get("cochera")),
            _safe_float(listing.get("expensas_ars")),
            float(price_usd),
            round(price_m2, 1),
            a.get("evaluation"),
            a.get("delta_pct"),
        ]
        ws.append(row)
        added += 1

    # Auto-width columns
    for col_idx in range(1, len(COLUMNS) + 1):
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = 18

    wb.save(HISTORY_FILE)
    logger.info(f"Saved {added} listings to {HISTORY_FILE}")


def _norte_price(listing: dict) -> tuple[Optional[str], Optional[float]]:
    if listing.get("price_usd"):
        return "USD", float(listing["price_usd"])
    if listing.get("price_ars"):
        return "ARS", float(listing["price_ars"])
    return None, None


def append_norte(norte_listings: list[dict], run_date: Optional[date] = None) -> None:
    """Append zona-norte listings a su propia hoja (no afecta el modelo de Recoleta)."""
    if not norte_listings:
        return
    today = run_date or date.today()
    wb = _ensure_workbook()
    if NORTE_SHEET not in wb.sheetnames:
        ws = wb.create_sheet(NORTE_SHEET)
        ws.append(NORTE_COLUMNS)
        for col_idx in range(1, len(NORTE_COLUMNS) + 1):
            ws.cell(row=1, column=col_idx).font = openpyxl.styles.Font(bold=True)
            ws.column_dimensions[get_column_letter(col_idx)].width = 18
    ws = wb[NORTE_SHEET]

    added = 0
    for l in norte_listings:
        currency, price = _norte_price(l)
        m2_cub = _safe_float(l.get("m2_cubiertos"))
        if not price:
            continue
        price_m2 = round(price / m2_cub, 1) if m2_cub else None
        ws.append([
            today.isoformat(), l.get("url"), l.get("id"), l.get("operation"),
            l.get("name"), l.get("address"), m2_cub,
            _safe_float(l.get("ambientes")), _safe_float(l.get("dormitorios")),
            currency, price, price_m2, _safe_float(l.get("expensas_ars")),
            ", ".join(l.get("amenities") or []),
        ])
        added += 1

    wb.save(HISTORY_FILE)
    logger.info(f"Saved {added} zona-norte listings to {HISTORY_FILE}")


def load_norte_history() -> dict[str, list[float]]:
    """url -> lista cronológica de precios (en la moneda original del aviso)."""
    if not HISTORY_FILE.exists():
        return {}
    wb = openpyxl.load_workbook(HISTORY_FILE, read_only=True)
    if NORTE_SHEET not in wb.sheetnames:
        return {}
    history: dict[str, list[float]] = {}
    for i, row in enumerate(wb[NORTE_SHEET].iter_rows(values_only=True)):
        if i == 0 or not row or not row[1]:
            continue
        price = _safe_float(row[10])
        if price:
            history.setdefault(row[1], []).append(price)
    return history


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Historical stats ─────────────────────────────────────────────────────────

def load_price_history() -> dict[str, list[float]]:
    """
    Returns {url: [price1, price2, ...]} with all historical prices per listing URL,
    sorted oldest→newest. Used to detect new listings and price drops.
    """
    if not HISTORY_FILE.exists():
        return {}

    try:
        wb = openpyxl.load_workbook(HISTORY_FILE, read_only=True, data_only=True)
        ws = wb[SHEET_NAME]
    except Exception:
        return {}

    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return {}

    header = [str(h) for h in rows[0]]
    try:
        url_col   = header.index("url")
        price_col = header.index("price_usd")
        date_col  = header.index("date")
    except ValueError:
        return {}

    # Collect (date, price) per URL, then sort by date
    history: dict[str, list[tuple]] = {}
    for row in rows[1:]:
        url   = row[url_col]   if url_col   < len(row) else None
        price = row[price_col] if price_col < len(row) else None
        dt    = row[date_col]  if date_col  < len(row) else None
        if not url or price is None:
            continue
        try:
            history.setdefault(url, []).append((dt, float(price)))
        except (TypeError, ValueError):
            pass

    return {
        url: [p for _, p in sorted(entries)]
        for url, entries in history.items()
    }


def clean_history(max_price_usd: float = 400_000) -> int:
    """Remove rows with price_usd > max_price_usd from history. Returns rows removed."""
    if not HISTORY_FILE.exists():
        return 0

    wb = openpyxl.load_workbook(HISTORY_FILE)
    ws = wb[SHEET_NAME]

    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return 0

    header = list(rows[0])
    try:
        price_col = header.index("price_usd")
    except ValueError:
        return 0

    # Rebuild sheet keeping only rows within budget
    to_keep = [header]
    removed = 0
    for row in rows[1:]:
        price = row[price_col] if price_col < len(row) else None
        try:
            if price is not None and float(price) > max_price_usd:
                removed += 1
                continue
        except (TypeError, ValueError):
            pass
        to_keep.append(row)

    if removed:
        ws.delete_rows(1, ws.max_row)
        for r in to_keep:
            ws.append(list(r))
        wb.save(HISTORY_FILE)
        logger.info(f"Cleaned {removed} rows > USD {max_price_usd:,.0f} from history")

    return removed


def load_zone_stats() -> dict[str, dict]:
    """
    Read history and compute per-zone stats:
      {
        'centro': {
          'count': 45,
          'median': 2850.0,
          'p25': 2400.0,
          'p75': 3200.0,
        },
        ...
      }
    Returns empty dict if history file doesn't exist.
    """
    if not HISTORY_FILE.exists():
        return {}

    try:
        wb = openpyxl.load_workbook(HISTORY_FILE, read_only=True, data_only=True)
        ws = wb[SHEET_NAME]
    except Exception as exc:
        logger.warning(f"Could not read history file: {exc}")
        return {}

    # Read header row
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return {}

    header = [str(h) for h in rows[0]]
    try:
        zone_col = header.index("zone")
        price_m2_col = header.index("price_usd_m2")
    except ValueError:
        return {}

    # Collect price/m² per zone (deduplicate by (date, url) to avoid re-adding same listing)
    url_col = header.index("url") if "url" in header else None
    date_col = header.index("date") if "date" in header else None

    zone_prices: dict[str, list[float]] = {}
    seen = set()

    for row in rows[1:]:
        zone = row[zone_col] if zone_col < len(row) else None
        price_m2 = row[price_m2_col] if price_m2_col < len(row) else None

        if not zone or price_m2 is None:
            continue

        # Deduplicate (same listing on different days is OK — it gives more weight to longer listings)
        # But avoid counting the same listing multiple times on the SAME day
        dedup_key = None
        if url_col is not None and date_col is not None:
            dedup_key = (row[date_col], row[url_col])
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

        try:
            price_m2_val = float(price_m2)
            if price_m2_val > 0:
                zone_prices.setdefault(zone, []).append(price_m2_val)
        except (TypeError, ValueError):
            pass

    stats = {}
    for zone, prices in zone_prices.items():
        if len(prices) < MIN_SAMPLES_FOR_STATS:
            logger.debug(f"Zone '{zone}': {len(prices)} samples < {MIN_SAMPLES_FOR_STATS} needed — skipping historical model")
            continue

        sorted_prices = sorted(prices)
        n = len(sorted_prices)
        p25_idx = max(0, int(n * 0.25) - 1)
        p75_idx = min(n - 1, int(n * 0.75))

        stats[zone] = {
            "count": n,
            "median": statistics.median(sorted_prices),
            "p25": sorted_prices[p25_idx],
            "p75": sorted_prices[p75_idx],
            "mean": statistics.mean(sorted_prices),
            "stdev": statistics.stdev(sorted_prices) if n >= 2 else 0,
        }
        logger.info(
            f"Zone '{zone}': {n} samples, "
            f"median={stats[zone]['median']:.0f}, "
            f"P25={stats[zone]['p25']:.0f}, "
            f"P75={stats[zone]['p75']:.0f} USD/m²"
        )

    return stats
