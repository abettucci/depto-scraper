from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from pydantic import ValidationError

from .dedupe import choose_representative, deduplicate
from .models import Listing, SavedSearch, is_allowed_host

SUPABASE_HOST_SUFFIX = "supabase.co"


class SupabaseStore:
    def __init__(self, url: str | None = None, service_key: str | None = None):
        self.url = (url or os.getenv("SUPABASE_URL") or "").rstrip("/")
        self.service_key = service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.hostname or not is_allowed_host(parsed.hostname, (SUPABASE_HOST_SUFFIX,)):
            raise ValueError("SUPABASE_URL debe ser una URL HTTPS de Supabase")
        if not self.service_key:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY es obligatorio")
        self.client = requests.Session()

    def list_searches(self) -> list[SavedSearch]:
        rows = self._request("GET", "saved_searches", params={"select": "*", "active": "eq.true"})
        searches = []
        for row in rows:
            try:
                searches.append(SavedSearch.model_validate(row))
            except ValidationError:
                # Do not log configuration contents: it can include locations and chat IDs.
                continue
        return searches

    def record_run(self, search: SavedSearch, listings: list[Listing], source_succeeded: bool) -> dict[str, list[Listing]]:
        known = self._request("GET", "listing_sources", params={"select": "*,listing_groups(*)", "search_id": f"eq.{search.id}"})
        known_by_key = {row["source_key"]: row for row in known}
        known_active = {row["source_key"] for row in known if row.get("active")}
        is_baseline = not search.baseline_completed and source_succeeded
        seen_keys: set[str] = set()
        changes: dict[str, list[Listing]] = defaultdict(list)

        for group in deduplicate(listings):
            representative = choose_representative(group)
            group_id = self._find_or_create_group(search, representative, known)
            for listing in group:
                seen_keys.add(listing.source_key)
                existing = known_by_key.get(listing.source_key)
                if existing is None:
                    row = self._request("POST", "listing_sources", json={"search_id": search.id, "group_id": group_id, "source_key": listing.source_key, "portal": listing.source.value, "listing_url": listing.url, "active": True}, prefer="return=representation")[0]
                    known_by_key[listing.source_key] = row
                    changes["new"].append(listing)
                    source_id = row["id"]
                else:
                    source_id = existing["id"]
                    was_active = bool(existing.get("active"))
                    if not was_active:
                        changes["available_again"].append(listing)
                    previous_price = ((existing.get("listing_groups") or {}).get("last_price"))
                    if previous_price is not None and listing.price is not None and str(previous_price) != str(listing.price):
                        changes["price_changed"].append(listing)
                    self._request("PATCH", "listing_sources", params={"id": f"eq.{source_id}"}, json={"active": True, "last_seen_at": datetime.now(timezone.utc).isoformat()})
                self._request("POST", "listing_snapshots", json=_snapshot_payload(source_id, listing))
            self._request("PATCH", "listing_groups", params={"id": f"eq.{group_id}"}, json=_group_payload(representative))

        for source_key in known_active - seen_keys:
            row = known_by_key[source_key]
            self._request("PATCH", "listing_sources", params={"id": f"eq.{row['id']}"}, json={"active": False})
            changes["unavailable"].append(_listing_from_known(row))
        if is_baseline:
            self._request("PATCH", "saved_searches", params={"id": f"eq.{search.id}"}, json={"baseline_completed": True})
            return {}
        return changes

    def _find_or_create_group(self, search: SavedSearch, listing: Listing, known: list[dict[str, Any]]) -> str:
        for row in known:
            group = row.get("listing_groups") or {}
            if group.get("fingerprint") == listing.fingerprint:
                return str(group["id"])
        row = self._request("POST", "listing_groups", json={"search_id": search.id, **_group_payload(listing)}, prefer="return=representation")[0]
        return str(row["id"])

    def _request(self, method: str, table: str, *, params: dict[str, str] | None = None, json: Any = None, prefer: str | None = None):
        headers = {"apikey": self.service_key, "Authorization": f"Bearer {self.service_key}", "Content-Type": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        response = self.client.request(method, f"{self.url}/rest/v1/{table}", params=params, json=json, headers=headers, timeout=20, allow_redirects=False)
        response.raise_for_status()
        return response.json() if response.content else []


def _group_payload(listing: Listing) -> dict[str, Any]:
    return {"fingerprint": listing.fingerprint, "title": listing.title, "address": listing.address, "last_price": str(listing.price) if listing.price is not None else None, "currency": listing.currency, "covered_m2": str(listing.covered_m2) if listing.covered_m2 is not None else None, "rooms": str(listing.rooms) if listing.rooms is not None else None}


def _snapshot_payload(source_id: str, listing: Listing) -> dict[str, Any]:
    return {"source_id": source_id, "price": str(listing.price) if listing.price is not None else None, "currency": listing.currency, "expenses": str(listing.expenses) if listing.expenses is not None else None, "rooms": str(listing.rooms) if listing.rooms is not None else None, "covered_m2": str(listing.covered_m2) if listing.covered_m2 is not None else None, "raw_title": listing.title, "raw_address": listing.address}


def _listing_from_known(row: dict[str, Any]) -> Listing:
    group = row.get("listing_groups") or {}
    return Listing(source=row["portal"], source_url=row["listing_url"], url=row["listing_url"], title=group.get("title") or "Aviso sin titulo", address=group.get("address"), price=group.get("last_price"), currency=group.get("currency"), rooms=group.get("rooms"), covered_m2=group.get("covered_m2"))
