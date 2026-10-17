from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

from .sources import fetch_source
from .searches import matches_filters
from .store import SupabaseStore
from .whatsapp import send_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
    run_monitor(
        supabase_url=os.getenv("SUPABASE_URL"),
        supabase_service_role_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
        whatsapp_sidecar_url=os.getenv("WHATSAPP_SIDECAR_URL"),
        waweb_shared_secret=os.getenv("WAWEB_SHARED_SECRET"),
        default_whatsapp_chat_id=os.getenv("DEFAULT_WHATSAPP_CHAT_ID"),
    )


def run_monitor(
    *,
    supabase_url: str | None,
    supabase_service_role_key: str | None,
    whatsapp_sidecar_url: str | None,
    waweb_shared_secret: str | None,
    default_whatsapp_chat_id: str | None,
) -> dict[str, int]:
    store = SupabaseStore(url=supabase_url, service_key=supabase_service_role_key)
    processed_searches = 0
    for search in store.list_searches():
        processed_searches += 1
        listings = []
        source_succeeded = False
        for source in search.source_urls:
            try:
                listings.extend(fetch_source(source))
                source_succeeded = True
            except Exception as error:
                logger.warning("source_fetch_failed portal=%s error_type=%s", source.portal.value, type(error).__name__)
        changes = store.record_run(search, [listing for listing in listings if matches_filters(listing, search.filters)], source_succeeded)
        chat_id = search.notify_chat_id or default_whatsapp_chat_id
        if chat_id and any(changes.values()):
            send_summary(chat_id, search.name, changes, sidecar_url=whatsapp_sidecar_url, secret=waweb_shared_secret)
            logger.info("summary_sent search_id=%s", search.id)
    return {"searches": processed_searches}


if __name__ == "__main__":
    main()
