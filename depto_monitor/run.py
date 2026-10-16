from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

from .sources import fetch_source
from .store import SupabaseStore
from .whatsapp import send_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
    store = SupabaseStore()
    for search in store.list_searches():
        listings = []
        source_succeeded = False
        for source in search.source_urls:
            try:
                listings.extend(fetch_source(source))
                source_succeeded = True
            except Exception as error:
                logger.warning("source_fetch_failed portal=%s error_type=%s", source.portal.value, type(error).__name__)
        changes = store.record_run(search, listings, source_succeeded)
        chat_id = search.notify_chat_id or os.getenv("DEFAULT_WHATSAPP_CHAT_ID")
        if chat_id and any(changes.values()):
            send_summary(chat_id, search.name, changes)
            logger.info("summary_sent search_id=%s", search.id)


if __name__ == "__main__":
    main()
