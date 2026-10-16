from __future__ import annotations

from collections import defaultdict

from .models import Listing


def deduplicate(listings: list[Listing]) -> list[list[Listing]]:
    groups: dict[str, list[Listing]] = defaultdict(list)
    for listing in listings:
        groups[listing.fingerprint].append(listing)
    return list(groups.values())


def choose_representative(group: list[Listing]) -> Listing:
    return max(group, key=lambda listing: sum(value is not None for value in (listing.price, listing.expenses, listing.rooms, listing.covered_m2, listing.address)))
