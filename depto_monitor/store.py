from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .dedupe import choose_representative, deduplicate
from .models import Listing, SavedSearch

DEFAULT_DATABASE_PATH = Path("data/depto-monitor.sqlite3")


class SQLiteStore:
    """Local persistence for one personal monitor installation."""

    def __init__(self, database_path: str | Path | None = None):
        self.database_path = Path(database_path or DEFAULT_DATABASE_PATH).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def save_search(self, search: SavedSearch) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO saved_searches (
                    id, user_id, name, filters_json, source_urls_json, notify_chat_id,
                    active, baseline_completed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    search.id,
                    search.user_id,
                    search.name,
                    _json(search.filters.model_dump(mode="json")),
                    _json([source.model_dump(mode="json") for source in search.source_urls]),
                    search.notify_chat_id,
                    int(search.active),
                    int(search.baseline_completed),
                    _now(),
                ),
            )

    def list_searches(self) -> list[SavedSearch]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, name, filters_json, source_urls_json, notify_chat_id,
                       active, baseline_completed
                FROM saved_searches
                WHERE active = 1
                ORDER BY created_at DESC
                """
            ).fetchall()
        searches = []
        for row in rows:
            try:
                searches.append(
                    SavedSearch.model_validate(
                        {
                            "id": row["id"],
                            "user_id": row["user_id"],
                            "name": row["name"],
                            "filters": json.loads(row["filters_json"]),
                            "source_urls": json.loads(row["source_urls_json"]),
                            "notify_chat_id": row["notify_chat_id"],
                            "active": bool(row["active"]),
                            "baseline_completed": bool(row["baseline_completed"]),
                        }
                    )
                )
            except (json.JSONDecodeError, ValidationError):
                continue
        return searches

    def list_search_summaries(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            searches = connection.execute(
                """
                SELECT saved_searches.id, saved_searches.name, saved_searches.created_at,
                       COUNT(listing_groups.id) AS listing_count
                FROM saved_searches
                LEFT JOIN listing_groups ON listing_groups.search_id = saved_searches.id
                WHERE saved_searches.active = 1
                GROUP BY saved_searches.id
                ORDER BY saved_searches.created_at DESC
                """
            ).fetchall()
            summaries = []
            for search in searches:
                listings = connection.execute(
                    """
                    SELECT title, address, last_price, currency, updated_at
                    FROM listing_groups
                    WHERE search_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 5
                    """,
                    (search["id"],),
                ).fetchall()
                summaries.append(
                    {
                        "id": search["id"],
                        "name": search["name"],
                        "created_at": search["created_at"],
                        "listing_count": search["listing_count"],
                        "listings": [dict(listing) for listing in listings],
                    }
                )
        return summaries

    def record_run(self, search: SavedSearch, listings: list[Listing], source_succeeded: bool) -> dict[str, list[Listing]]:
        with self._connection() as connection:
            known = connection.execute(
                """
                SELECT listing_sources.id AS source_id, listing_sources.source_key,
                       listing_sources.portal, listing_sources.listing_url,
                       listing_sources.active, listing_groups.id AS group_id,
                       listing_groups.fingerprint, listing_groups.title,
                       listing_groups.address, listing_groups.last_price,
                       listing_groups.currency, listing_groups.covered_m2,
                       listing_groups.rooms
                FROM listing_sources
                JOIN listing_groups ON listing_groups.id = listing_sources.group_id
                WHERE listing_sources.search_id = ?
                """,
                (search.id,),
            ).fetchall()
            known_by_key = {row["source_key"]: row for row in known}
            groups_by_fingerprint = {row["fingerprint"]: row["group_id"] for row in known}
            known_active = {row["source_key"] for row in known if row["active"]}
            is_baseline = not search.baseline_completed and source_succeeded
            seen_keys: set[str] = set()
            changes: dict[str, list[Listing]] = defaultdict(list)

            for group in deduplicate(listings):
                representative = choose_representative(group)
                group_id = groups_by_fingerprint.get(representative.fingerprint)
                if group_id is None:
                    group_id = str(uuid4())
                    groups_by_fingerprint[representative.fingerprint] = group_id
                    connection.execute(
                        """
                        INSERT INTO listing_groups (
                            id, search_id, fingerprint, title, address, last_price, currency,
                            covered_m2, rooms, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (group_id, search.id, *_group_values(representative), _now()),
                    )

                for listing in group:
                    seen_keys.add(listing.source_key)
                    existing = known_by_key.get(listing.source_key)
                    if existing is None:
                        source_id = str(uuid4())
                        connection.execute(
                            """
                            INSERT INTO listing_sources (
                                id, search_id, group_id, source_key, portal, listing_url,
                                active, last_seen_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (source_id, search.id, group_id, listing.source_key, listing.source.value, listing.url, 1, _now()),
                        )
                        changes["new"].append(listing)
                    else:
                        source_id = existing["source_id"]
                        if not existing["active"]:
                            changes["available_again"].append(listing)
                        if existing["last_price"] is not None and listing.price is not None and str(existing["last_price"]) != str(listing.price):
                            changes["price_changed"].append(listing)
                        connection.execute(
                            "UPDATE listing_sources SET active = 1, last_seen_at = ? WHERE id = ?",
                            (_now(), source_id),
                        )
                    connection.execute(
                        """
                        INSERT INTO listing_snapshots (
                            id, source_id, captured_at, price, currency, expenses, rooms,
                            covered_m2, raw_title, raw_address
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (str(uuid4()), source_id, _now(), *_snapshot_values(listing)),
                    )
                connection.execute(
                    """
                    UPDATE listing_groups
                    SET title = ?, address = ?, last_price = ?, currency = ?, covered_m2 = ?,
                        rooms = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (*_group_values(representative)[1:], _now(), group_id),
                )

            for source_key in known_active - seen_keys:
                row = known_by_key[source_key]
                connection.execute("UPDATE listing_sources SET active = 0 WHERE id = ?", (row["source_id"],))
                changes["unavailable"].append(_listing_from_row(row))
            if is_baseline:
                connection.execute("UPDATE saved_searches SET baseline_completed = 1 WHERE id = ?", (search.id,))
                return {}
        return changes

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialise(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS saved_searches (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    filters_json TEXT NOT NULL,
                    source_urls_json TEXT NOT NULL,
                    notify_chat_id TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    baseline_completed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS listing_groups (
                    id TEXT PRIMARY KEY,
                    search_id TEXT NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL,
                    title TEXT NOT NULL,
                    address TEXT,
                    last_price TEXT,
                    currency TEXT,
                    covered_m2 TEXT,
                    rooms TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(search_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS listing_sources (
                    id TEXT PRIMARY KEY,
                    search_id TEXT NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
                    group_id TEXT NOT NULL REFERENCES listing_groups(id) ON DELETE CASCADE,
                    source_key TEXT NOT NULL,
                    portal TEXT NOT NULL,
                    listing_url TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(search_id, source_key)
                );
                CREATE TABLE IF NOT EXISTS listing_snapshots (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES listing_sources(id) ON DELETE CASCADE,
                    captured_at TEXT NOT NULL,
                    price TEXT,
                    currency TEXT,
                    expenses TEXT,
                    rooms TEXT,
                    covered_m2 TEXT,
                    raw_title TEXT,
                    raw_address TEXT
                );
                """
            )


def _group_values(listing: Listing) -> tuple[str | None, ...]:
    return (
        listing.fingerprint,
        listing.title,
        listing.address,
        str(listing.price) if listing.price is not None else None,
        listing.currency,
        str(listing.covered_m2) if listing.covered_m2 is not None else None,
        str(listing.rooms) if listing.rooms is not None else None,
    )


def _snapshot_values(listing: Listing) -> tuple[str | None, ...]:
    return (
        str(listing.price) if listing.price is not None else None,
        listing.currency,
        str(listing.expenses) if listing.expenses is not None else None,
        str(listing.rooms) if listing.rooms is not None else None,
        str(listing.covered_m2) if listing.covered_m2 is not None else None,
        listing.title,
        listing.address,
    )


def _listing_from_row(row: sqlite3.Row) -> Listing:
    return Listing(
        source=row["portal"],
        source_url=row["listing_url"],
        url=row["listing_url"],
        title=row["title"] or "Aviso sin título",
        address=row["address"],
        price=row["last_price"],
        currency=row["currency"],
        rooms=row["rooms"],
        covered_m2=row["covered_m2"],
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
