from __future__ import annotations

import os
from urllib.parse import urlparse

import requests

from .models import Listing, is_allowed_host


def send_summary(chat_id: str, search_name: str, changes: dict[str, list[Listing]]) -> bool:
    sidecar_url = (os.getenv("WHATSAPP_SIDECAR_URL") or "").rstrip("/")
    secret = os.getenv("WAWEB_SHARED_SECRET") or ""
    parsed = urlparse(sidecar_url)
    if not sidecar_url or parsed.scheme != "https" or not parsed.hostname or not is_allowed_host(parsed.hostname, ("up.railway.app",)):
        raise ValueError("WHATSAPP_SIDECAR_URL debe ser HTTPS y pertenecer al sidecar Railway")
    if not secret:
        raise ValueError("WAWEB_SHARED_SECRET es obligatorio")
    text = format_summary(search_name, changes)
    if not text:
        return False
    response = requests.post(f"{sidecar_url}/send", headers={"Authorization": f"Bearer {secret}"}, json={"chat_id": chat_id, "chat_type": "private", "text": text}, timeout=15, allow_redirects=False)
    response.raise_for_status()
    return True


def format_summary(search_name: str, changes: dict[str, list[Listing]]) -> str:
    labels = {"new": "Nuevos", "price_changed": "Cambios de precio", "available_again": "Disponibles otra vez", "unavailable": "Ya no disponibles"}
    blocks = [f"🏠 *{search_name}*"]
    for key, label in labels.items():
        entries = changes.get(key, [])
        if not entries:
            continue
        blocks.append(f"\n*{label} ({len(entries)})*")
        for listing in entries[:10]:
            price = f"{listing.currency or ''} {listing.price or 's/d'}".strip()
            location = f" — {listing.address}" if listing.address else ""
            blocks.append(f"• {listing.title}{location}\n  {price}\n  {listing.url}")
    return "\n".join(blocks) if len(blocks) > 1 else ""
