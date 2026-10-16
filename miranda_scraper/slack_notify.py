"""
Formatea y envía el resumen diario al webhook de Slack.
"""

import logging
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger(__name__)

MAX_YELLOW_IN_MESSAGE = 6   # cap amarillos; rojos no se muestran
SITE_URL = (
    "https://www.mirandabosch.com/residencial/"
    "venta-recoleta-50-150-m2-cubiertos-quicksearch-rec"
)

ESTADO_LABELS = {
    "a_estrenar": "⭐ A estrenar",
    "reciclado": "✨ Reciclado",
    "a_reciclar": "🔨 A reciclar",
    "normal": "",
}


def _fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "s/d"
    return f"USD {int(value):,}".replace(",", ".")


def _fmt_number(value, decimals=0) -> str:
    if value is None:
        return "s/d"
    try:
        return f"{float(value):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(value)


def _listing_tag(listing: dict, price_history: dict) -> str:
    """Return 🆕, 📉, or '' based on price history."""
    url = listing.get("url") or ""
    if not url or not price_history:
        return "🆕"  # sin historial = todo es nuevo
    past = price_history.get(url)
    if not past:
        return "🆕"
    current = listing.get("price_usd")
    if current and past and float(current) < past[-1] * 0.98:  # bajó > 2%
        return "📉"
    return ""


def _listing_block(listing: dict, price_history: dict = None) -> str:
    """Format one listing as a Slack mrkdwn text block."""
    a = listing.get("analysis") or {}
    emoji = a.get("emoji", "⚪")
    is_opportunity = a.get("opportunity", False)

    name = listing.get("name") or listing.get("rounded_address") or "Sin nombre"
    address = listing.get("address") or ""
    url = listing.get("url") or ""

    price_usd = listing.get("price_usd")
    m2_cub = listing.get("m2_cubiertos")
    m2_tot = listing.get("m2_total")
    ambientes = listing.get("ambientes")
    dormitorios = listing.get("dormitorios")
    piso = listing.get("piso")
    orientacion = listing.get("orientacion") or ""
    estado_detected = listing.get("estado_detected", "normal")
    estado_label = ESTADO_LABELS.get(estado_detected, "")
    cochera = listing.get("cochera", False)
    expensas_ars = listing.get("expensas_ars")

    # m² line
    m2_parts = []
    if m2_cub:
        m2_parts.append(f"{_fmt_number(m2_cub)} m² cub.")
    if m2_tot and m2_tot != m2_cub:
        m2_parts.append(f"{_fmt_number(m2_tot)} m² tot.")
    m2_str = " / ".join(m2_parts) if m2_parts else "m² s/d"

    # ambientes / dorm
    amb_str = ""
    if ambientes:
        amb_str = f"{int(float(ambientes))} amb."
        if dormitorios:
            amb_str += f" / {int(float(dormitorios))} dorm."

    # piso
    piso_str = f"Piso {piso}" if piso is not None else ""

    # price analysis
    actual_m2 = a.get("actual_price_m2")
    ref_m2 = a.get("ref_price_m2")
    delta = a.get("delta_pct")
    delta_str = ""
    if delta is not None:
        sign = "+" if delta >= 0 else ""
        delta_str = f"{sign}{delta:.1f}%"

    # expensas
    exp_str = ""
    if expensas_ars:
        try:
            exp_val = int(float(expensas_ars))
            exp_str = f"Exp: ${exp_val:,}".replace(",", ".")
        except (TypeError, ValueError):
            pass

    # source badge
    source = listing.get("source", "")
    source_badge = " `MB`" if source == "miranda_bosch" else " `ZP`" if source == "zonaprop" else " `RM`" if source == "remax" else ""

    # fire signals count (shown for fire tier)
    fire_signals = a.get("fire_signals", 0)
    score = a.get("score", 0)
    evaluation = a.get("evaluation", "")

    # novedad: 🆕 primera vez, 📉 bajó de precio
    tag = _listing_tag(listing, price_history or {})
    tag_str = f"  {tag}" if tag else ""

    # build title line
    if evaluation == "fire":
        signals_str = f"  _{fire_signals}/5 señales_"
        if url:
            title = f"🔥 *<{url}|{name}>*{source_badge}{signals_str}{tag_str}"
        else:
            title = f"🔥 *{name}*{source_badge}{signals_str}{tag_str}"
    else:
        if url:
            title = f"{emoji} *<{url}|{name}>*{source_badge}{tag_str}"
        else:
            title = f"{emoji} *{name}*{source_badge}{tag_str}"
        if is_opportunity:
            title += "  _(a reciclar — negociar!)_"

    lines = [title]

    # address + piso + orientacion
    # Avoid repeating "Piso X" if it's already in the address string
    detail_parts = []
    if address:
        detail_parts.append(address)
    if piso_str and address and "piso" not in address.lower():
        detail_parts.append(piso_str)
    if orientacion:
        detail_parts.append(f"Orientación {orientacion}")
    if detail_parts:
        lines.append("  " + " · ".join(detail_parts))

    # size + ambientes + estado + cochera
    prop_parts = [p for p in [m2_str, amb_str, estado_label] if p]
    if cochera:
        prop_parts.append("🅿 Cochera")
    if exp_str:
        prop_parts.append(exp_str)
    if prop_parts:
        lines.append("  " + " · ".join(prop_parts))

    # price analysis
    price_parts = [_fmt_usd(price_usd)]
    if actual_m2 and ref_m2:
        price_parts.append(
            f"USD {int(actual_m2):,}/m² cub. (ref: {int(ref_m2):,}) {delta_str}".replace(",", ".")
        )
    elif actual_m2:
        price_parts.append(f"USD {int(actual_m2):,}/m² cub.".replace(",", "."))

    zone_label = a.get("zone_label", "")
    if zone_label:
        price_parts.append(zone_label)

    lines.append("  " + "  |  ".join(price_parts))

    return "\n".join(lines)


MAX_NORTE_PER_OPERATION = 8


def _fmt_money(listing: dict, usd_ars_rate: Optional[float]) -> str:
    if listing.get("price_usd"):
        return _fmt_usd(listing["price_usd"])
    ars = listing.get("price_ars")
    if not ars:
        return "s/d"
    text = f"$ {int(ars):,}".replace(",", ".")
    if usd_ars_rate:
        text += f" (~{_fmt_usd(ars / usd_ars_rate)})"
    return text


def _norte_tag(listing: dict, norte_history: dict) -> str:
    past = (norte_history or {}).get(listing.get("url") or "")
    if not past:
        return "🆕"
    current = listing.get("price_usd") or listing.get("price_ars")
    if current and float(current) < past[-1] * 0.98:
        return "📉"
    return ""


def _norte_block(listing: dict, norte_history: dict, usd_ars_rate: Optional[float]) -> str:
    name = listing.get("name") or listing.get("address") or "Sin nombre"
    url = listing.get("url") or ""
    tag = _norte_tag(listing, norte_history)
    badge = " `ZP`" if listing.get("source") == "zonaprop" else " `RM`" if listing.get("source") == "remax" else ""
    title = f"🏢 *<{url}|{name}>*{badge}" if url else f"🏢 *{name}*{badge}"
    if tag:
        title += f"  {tag}"

    m2 = listing.get("m2_cubiertos")
    detail = [p for p in [
        listing.get("address"),
        f"{_fmt_number(m2)} m² cub." if m2 else None,
        f"{int(float(listing['ambientes']))} amb." if listing.get("ambientes") else None,
        f"Exp: ${int(float(listing['expensas_ars'])):,}".replace(",", ".") if listing.get("expensas_ars") else None,
        "🅿 Cochera" if listing.get("cochera") else None,
    ] if p]

    price = _fmt_money(listing, usd_ars_rate)
    if listing.get("operation") == "venta" and listing.get("price_usd") and m2:
        price += f"  |  USD {int(listing['price_usd'] / float(m2)):,}/m² cub.".replace(",", ".")
    elif listing.get("operation") == "alquiler":
        price += " /mes"

    lines = [title, "  " + " · ".join(detail), "  " + price]
    if listing.get("amenities"):
        lines.append("  " + ", ".join(listing["amenities"]))
    return "\n".join(lines)


def _norte_sort_key(listing: dict, norte_history: dict):
    # nuevos y bajas primero, después el más barato (por USD/m² en venta, por precio en alquiler)
    new_first = 0 if _norte_tag(listing, norte_history) else 1
    m2 = float(listing.get("m2_cubiertos") or 0)
    price = listing.get("price_usd") or listing.get("price_ars") or 0
    cheap = price / m2 if (listing.get("operation") == "venta" and m2) else price
    return (new_first, cheap)


def build_norte_section(
    norte_listings: list[dict],
    norte_history: dict = None,
    usd_ars_rate: Optional[float] = None,
) -> str:
    """Sección 'Complejos zona norte' con venta y alquiler. '' si no hay avisos."""
    if not norte_listings:
        return ""
    parts = [f"{'─' * 40}\n🏖 *Complejos zona norte — San Isidro / el bajo (con amenities)*"]
    for operation, title in (("venta", "Venta"), ("alquiler", "Alquiler")):
        group = [l for l in norte_listings if l.get("operation") == operation]
        if not group:
            continue
        group.sort(key=lambda l: _norte_sort_key(l, norte_history))
        shown = group[:MAX_NORTE_PER_OPERATION]
        blocks = "\n\n".join(_norte_block(l, norte_history, usd_ars_rate) for l in shown)
        extra = len(group) - len(shown)
        more = f"\n_+{extra} más sin mostrar_" if extra > 0 else ""
        parts.append(f"*{title}* ({len(group)})\n{blocks}{more}")
    return "\n\n".join(parts)


def build_message(
    analyzed_listings: list[dict],
    price_history: dict = None,
    warnings: list[str] = None,
    norte_listings: list[dict] = None,
    norte_history: dict = None,
    usd_ars_rate: Optional[float] = None,
) -> str:
    """Build the full Slack message text."""
    today = date.today().strftime("%d/%m/%Y")

    # Count by evaluation
    counts: dict[str, int] = {}
    for l in analyzed_listings:
        a = l.get("analysis") or {}
        ev = a.get("evaluation") or "none"
        counts[ev] = counts.get(ev, 0) + 1

    total = len(analyzed_listings)
    fire_count = counts.get("fire", 0)
    reciclar_opps = sum(1 for l in analyzed_listings if (l.get("analysis") or {}).get("opportunity"))

    mb_count = sum(1 for l in analyzed_listings if l.get("source") == "miranda_bosch")
    zp_count = sum(1 for l in analyzed_listings if l.get("source") == "zonaprop")
    rm_count = sum(1 for l in analyzed_listings if l.get("source") == "remax")
    source_parts = []
    if mb_count: source_parts.append(f"`MB` {mb_count}")
    if zp_count: source_parts.append(f"`ZP` {zp_count}")
    if rm_count: source_parts.append(f"`RM` {rm_count}")
    source_line = " · ".join(source_parts) + " · " if source_parts else ""

    header = f"🏙 *Recoleta Venta — MB + ZP + RM | {today}*\n_{source_line}{total} propiedades"
    if fire_count:
        header += f" · 🔥 {fire_count} oportunidad{'es reales' if fire_count > 1 else ' real'}"
    header += (
        f" · 🟢 {counts.get('green', 0)}"
        f" · 🟡 {counts.get('yellow', 0)}"
        f" · 🔴 {counts.get('red', 0)}"
    )
    if reciclar_opps:
        header += f" · 🔨 {reciclar_opps} a reciclar"
    header += "_"

    # ── Selección inteligente de listings ───────────────────────────────────
    # Escala por zona: más lejos del centro → más exigente para aparecer.
    # Rojos nunca se muestran (caro = no es oportunidad).
    red_count = counts.get("red", 0)

    def _zone(l):
        return (l.get("analysis") or {}).get("zone", "exterior")

    def _ev(l):
        return (l.get("analysis") or {}).get("evaluation")

    def _delta(l):
        return (l.get("analysis") or {}).get("delta_pct", 0)

    def _should_show_green(l):
        z = _zone(l)
        if z in ("centro", "media"):
            return True
        # Exterior: solo si descuento excepcional (> 35%)
        return _delta(l) < -35

    # 1. Fire: siempre se muestran
    fire_shown = [l for l in analyzed_listings if _ev(l) == "fire"]

    # 2. Green: MB siempre (si cumple zona); ZP/RM máximo 4 en total (los mejores)
    green_all  = [l for l in analyzed_listings if _ev(l) == "green"]
    mb_greens  = [l for l in green_all if l.get("source") == "miranda_bosch" and _should_show_green(l)]
    ext_greens = [l for l in green_all if l.get("source") != "miranda_bosch" and _should_show_green(l)]
    green_shown  = mb_greens + ext_greens[:4]
    green_hidden = len(green_all) - len(green_shown)

    # 3. Yellow: solo centro/media, máx por zona
    #    centro → 4, media → 2, exterior → 0
    yellow_all = [l for l in analyzed_listings if _ev(l) == "yellow"]
    yellow_centro = [l for l in yellow_all if _zone(l) == "centro"][:4]
    yellow_media  = [l for l in yellow_all if _zone(l) == "media"][:2]
    yellow_shown  = yellow_centro + yellow_media
    hidden_yellow = len(yellow_all) - len(yellow_shown)

    shown = fire_shown + green_shown + yellow_shown

    blocks = [_listing_block(l, price_history) for l in shown if l.get("analysis")]
    body = "\n\n".join(blocks)

    message = f"{header}\n{'─' * 40}\n{body}"

    if warnings:
        warn_lines = "\n".join(f"• {w}" for w in warnings)
        message = f"⚠️ *Fuentes con problemas:*\n{warn_lines}\n\n{message}"

    footer_parts = []
    if hidden_yellow or green_hidden:
        omitted = hidden_yellow + green_hidden
        footer_parts.append(f"{omitted} propiedades en zona exterior/precio moderado")
    if red_count:
        footer_parts.append(f"{red_count} caros 🔴")
    if footer_parts:
        message += f"\n\n_No mostrados: {' · '.join(footer_parts)}. <{SITE_URL}|Ver todas en MB>_"

    # Zona norte va al final: tiene su propio formato y no pasa por el modelo de Recoleta
    norte_section = build_norte_section(norte_listings or [], norte_history, usd_ars_rate)
    if norte_section:
        message += f"\n\n{norte_section}"

    return message


def send_to_slack(
    webhook_url: str,
    listings: list[dict],
    price_history: dict = None,
    warnings: list[str] = None,
    norte_listings: list[dict] = None,
    norte_history: dict = None,
    usd_ars_rate: Optional[float] = None,
) -> bool:
    """Send formatted message to Slack webhook. Returns True on success."""
    message = build_message(
        listings, price_history, warnings, norte_listings, norte_history, usd_ars_rate
    )

    # Print to console for verification
    print("\n" + "=" * 60)
    print("SLACK MESSAGE PREVIEW:")
    print("=" * 60)
    print(message)
    print("=" * 60 + "\n")

    if not webhook_url:
        logger.warning("No SLACK_WEBHOOK_URL set — message printed but not sent")
        return False

    try:
        resp = requests.post(
            webhook_url,
            json={"text": message},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Slack message sent successfully")
        return True
    except requests.RequestException as exc:
        logger.error(f"Failed to send Slack message: {exc}")
        return False
