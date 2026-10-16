"""
Orquestador principal del scraper de Recoleta (Miranda Bosch + Zonaprop + RE/MAX).

Uso:
    python main.py          → corre una vez ahora
    python main.py --loop   → corre en bucle (usando schedule) - útil para tests
    python main.py --setup-scheduler  → registra la tarea en Windows Task Scheduler
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from common import MAX_PRICE_USD

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path(__file__).parent / "scraper.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("main")

# ── Load .env ────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _dedupe(listings: list[dict]) -> list[dict]:
    """
    Quita duplicados (mismo depto listado por dos agentes/portales).
    Clave = (operación, dirección, precio, m² cubiertos, piso) para no colapsar unidades
    distintas del mismo edificio que comparten precio. Se queda con la primera aparición.
    """
    import re as _re

    def _norm(s: str) -> str:
        return _re.sub(r"\s+", " ", (s or "").lower().strip())

    seen: set = set()
    out = []
    for l in listings:
        addr = _norm(l.get("address") or l.get("rounded_address") or "")
        price = l.get("price_usd") or l.get("price_ars")
        key = (l.get("operation"), addr, price, l.get("m2_cubiertos"), l.get("piso"))
        if addr and price:
            if key in seen:
                logger.debug(f"Dedup skip: {key}")
                continue
            seen.add(key)
        out.append(l)
    return out


# ── Core job ─────────────────────────────────────────────────────────────────

def run_job(dry_run: bool = False):
    """Full pipeline: scrape → analyze → notify. dry_run=True skips Slack."""
    logger.info("=" * 60)
    logger.info(f"Starting scrape job at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    from scraper import scrape_all
    from zonaprop_scraper import scrape_zonaprop, scrape_zonaprop_norte
    from remax_scraper import scrape_remax, scrape_remax_norte
    from price_model import analyze_all, get_usd_ars_rate
    from slack_notify import send_to_slack
    from data_logger import (
        load_zone_stats, append_listings, clean_history, load_price_history,
        append_norte, load_norte_history,
    )

    # 0. Limpiar history de registros fuera del filtro de precio
    clean_history(max_price_usd=MAX_PRICE_USD)

    # 1. Scrape — Miranda Bosch first, then Zonaprop, then RE/MAX.
    #    Cada fuente es independiente: si una falla o viene vacía, se avisa y se sigue.
    warnings: list[str] = []

    def _run_source(label: str, fn) -> list[dict]:
        logger.info(f"Scraping {label}…")
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — una fuente caída no debe tumbar el job
            logger.exception(f"{label} failed")
            warnings.append(f"{label}: falló el scraping ({type(exc).__name__}: {exc})")
            return []
        logger.info(f"  {label}: {len(result)} listings")
        if not result:
            warnings.append(f"{label}: 0 listings (¿bloqueo o cambio en el sitio?)")
        return result

    mb_listings = _run_source("Miranda Bosch", scrape_all)
    zp_listings = _run_source("Zonaprop", scrape_zonaprop)
    rm_listings = _run_source("RE/MAX", scrape_remax)

    # Zona norte (complejos con amenities): sección aparte, sin modelo de precios ni dedup
    # con Recoleta. Se alerta por separado si venta o alquiler vienen vacíos.
    rm_norte = _run_source("RE/MAX zona norte", scrape_remax_norte)
    zp_norte = _run_source("Zonaprop zona norte", scrape_zonaprop_norte)
    norte_listings = _dedupe(rm_norte + zp_norte)
    if len(norte_listings) != len(rm_norte) + len(zp_norte):
        logger.info(f"Zona norte: deduped {len(rm_norte) + len(zp_norte) - len(norte_listings)} duplicado(s)")
    for op in ("venta", "alquiler"):
        if norte_listings and not any(l.get("operation") == op for l in norte_listings):
            warnings.append(f"Zona norte: 0 listings de {op}")

    # Tag sources and merge (MB first so it appears first in Slack)
    for l in mb_listings:
        l.setdefault("source", "miranda_bosch")
    listings = mb_listings + zp_listings + rm_listings

    # Mismo tope de precio para las tres fuentes (RE/MAX y ZP ya lo filtran)
    within_cap = [l for l in listings if (l.get("price_usd") or 0) <= MAX_PRICE_USD]
    if len(within_cap) != len(listings):
        logger.info(f"Dropped {len(listings) - len(within_cap)} listing(s) over USD {MAX_PRICE_USD:,}")
    listings = within_cap

    deduped = _dedupe(listings)

    removed = len(listings) - len(deduped)
    if removed:
        logger.info(f"Deduped {removed} duplicate listing(s)")
    listings = deduped
    logger.info(f"Total: {len(listings)} listings")

    # 2. Raw JSON (solo a nivel DEBUG)
    logger.debug("RAW SCRAPED DATA (JSON):\n%s", json.dumps(listings, ensure_ascii=False, indent=2, default=str))

    # 3. Get exchange rate + historical zone stats
    usd_ars_rate = get_usd_ars_rate()
    logger.info(f"USD/ARS rate: {usd_ars_rate:.0f}")

    zone_stats = load_zone_stats()
    if zone_stats:
        logger.info(f"Historical model active for zones: {list(zone_stats.keys())}")
    else:
        logger.info("No historical data yet — using fixed reference prices")

    price_history = load_price_history()
    logger.info(f"Price history loaded: {len(price_history)} tracked URLs")

    # 4. Analyze prices (using historical model when available)
    analyzed = analyze_all(listings, usd_ars_rate, zone_stats)

    # 4a. Zona norte: historial propio (hoja "norte") y tags de novedad
    norte_history = load_norte_history()
    append_norte(norte_listings)

    # 4b. Append today's data to history AFTER analysis (so we log the evaluations)
    append_listings(analyzed)

    # 5. Print analysis summary
    print("\n" + "=" * 60)
    print("PRICE ANALYSIS SUMMARY:")
    print("=" * 60)
    for l in analyzed:
        a = l.get("analysis")
        if a:
            print(
                f"{a['emoji']} {l.get('name') or l.get('address'):30s} "
                f"USD {int(l.get('price_usd',0)):>8,} | "
                f"{int(a['actual_price_m2']):>5} vs {int(a['ref_price_m2']):>5} USD/m² | "
                f"Δ {a['delta_pct']:+.1f}% | "
                f"{l.get('estado_detected',''):10} | "
                f"{'🔥' if a.get('opportunity') else ''}"
            )
        else:
            print(
                f"⚪ {l.get('name') or l.get('address'):30s} — sin precio"
            )
    print("=" * 60 + "\n")

    # 6. Send to Slack
    if dry_run:
        logger.info("Job complete — dry run, Slack skipped")
    else:
        success = send_to_slack(
            SLACK_WEBHOOK_URL, analyzed, price_history, warnings,
            norte_listings, norte_history, usd_ars_rate,
        )
        if success:
            logger.info("Job complete — Slack message sent")
        else:
            logger.info("Job complete — Slack not sent (no webhook configured)")

    logger.info("=" * 60)
    return analyzed


# ── Scheduler setup (Windows Task Scheduler) ─────────────────────────────────

def setup_windows_scheduler():
    """Register a daily Windows Task Scheduler task at 8:00 AM Mon-Sat."""
    import subprocess

    script_path = Path(__file__).resolve()
    python_exe = sys.executable
    task_name = "MirandaBoschScraper"
    working_dir = script_path.parent

    # Build schtasks command
    cmd = [
        "schtasks", "/create",
        "/tn", task_name,
        "/tr", f'"{python_exe}" "{script_path}"',
        "/sc", "weekly",
        "/d", "MON,TUE,WED,THU,FRI,SAT",
        "/st", "08:00",
        "/sd", datetime.now().strftime("%m/%d/%Y"),
        "/f",  # overwrite if exists
    ]

    print(f"\nRegistering Windows Task Scheduler task: {task_name}")
    print(f"Command: {' '.join(cmd)}\n")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✓ Task '{task_name}' scheduled successfully!")
            print(f"  Runs: Mon–Sat at 08:00 AM")
            print(f"  Script: {script_path}")
            print(f"\n  To verify: schtasks /query /tn {task_name}")
            print(f"  To remove: schtasks /delete /tn {task_name} /f")
        else:
            print(f"✗ Failed to schedule task:")
            print(result.stderr)
            print("\nAlternatively, run manually:")
            print(f"  {python_exe} {script_path}")
    except FileNotFoundError:
        print("schtasks not available. To schedule manually:")
        print(f"  1. Open Task Scheduler (taskschd.msc)")
        print(f"  2. Create Basic Task → Daily → 08:00")
        print(f"  3. Action: Start a program")
        print(f"     Program: {python_exe}")
        print(f"     Arguments: {script_path}")
        print(f"     Start in: {working_dir}")


# ── Loop mode (dev/testing) ───────────────────────────────────────────────────

def run_with_schedule():
    """Run with the `schedule` library every day at 8:00."""
    try:
        import schedule
        import time
    except ImportError:
        print("Install schedule: pip install schedule")
        sys.exit(1)

    schedule.every().day.at("08:00").do(run_job)
    print(f"Scheduler started — will run at 08:00 daily. Press Ctrl+C to stop.")
    run_job()  # run immediately on start
    while True:
        schedule.run_pending()
        time.sleep(60)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Miranda Bosch Daily Scraper")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously with schedule library (dev mode)",
    )
    parser.add_argument(
        "--setup-scheduler",
        action="store_true",
        help="Register Windows Task Scheduler task (run once as admin)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run full pipeline but skip sending to Slack",
    )
    args = parser.parse_args()

    if args.setup_scheduler:
        setup_windows_scheduler()
    elif args.loop:
        run_with_schedule()
    else:
        run_job(dry_run=args.dry_run)
