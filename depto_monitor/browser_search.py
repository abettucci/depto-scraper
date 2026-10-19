from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import Listing, Portal, SourceURL, validate_portal_url
from .sources import parse_json_ld, parse_roomix_search_html

BROWSER_PAGE_TIMEOUT_MS = 20_000
BROWSER_REVIEW_SECONDS = 40
MAX_BROWSER_HTML_CHARS = 2_000_000
MAX_BROWSER_LINKS = 350
MAX_BROWSER_TEXT_CHARS = 1_500
PROFILE_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "browser-profile"

_PRICE = re.compile(r"(?P<currency>USD|U\$S|US\$|ARS|\$)\s*(?P<amount>\d(?:[\d.,]*\d)?)", re.IGNORECASE)
_ROOMS = re.compile(r"(?P<rooms>\d+)\s*amb(?:ientes?)?\b|(?P<studio>monoambiente)", re.IGNORECASE)
_BEDROOMS = re.compile(r"(?P<bedrooms>\d+)\s*(?:dorm|hab)\b", re.IGNORECASE)
_BATHROOMS = re.compile(r"(?P<bathrooms>\d+)\s*baños?\b", re.IGNORECASE)
_AREAS = re.compile(r"(?P<area>\d[\d.,]*)\s*m(?:²|2)\b", re.IGNORECASE)


class BrowserAutomationUnavailableError(RuntimeError):
    """Playwright or its Chromium binary is not installed locally."""


class BrowserSearchBusyError(RuntimeError):
    """Another assisted browser search is already using the local profile."""


class BrowserAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    href: str = Field(min_length=12, max_length=2_048)
    text: str = Field(default="", max_length=MAX_BROWSER_TEXT_CHARS)


@dataclass(frozen=True)
class BrowserSourceResult:
    source: SourceURL
    listings: list[Listing]
    status: str
    error_type: str | None = None
    http_status: int | None = None


def fetch_sources_in_browser(sources: list[SourceURL]) -> list[BrowserSourceResult]:
    """Read already-rendered result pages in a visible local Chromium session.

    This deliberately uses a normal, visible browser without custom headers,
    proxying, stealth configuration, or copied cookies. The user can complete a
    portal's own verification in the displayed window before extraction begins.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise BrowserAutomationUnavailableError("playwright is not installed") from error

    PROFILE_DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    PROFILE_DIRECTORY.chmod(0o700)
    results: list[BrowserSourceResult] = []
    opened_pages: list[tuple[SourceURL, Any, int | None]] = []

    with _exclusive_profile_lock():
        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(PROFILE_DIRECTORY),
                    headless=False,
                    accept_downloads=False,
                )
            except PlaywrightError as error:
                raise BrowserAutomationUnavailableError("chromium is not installed") from error
            try:
                for index, source in enumerate(sources):
                    page = context.pages[0] if index == 0 and context.pages else context.new_page()
                    try:
                        response = page.goto(source.url, wait_until="domcontentloaded", timeout=BROWSER_PAGE_TIMEOUT_MS)
                        opened_pages.append((source, page, response.status if response is not None else None))
                    except PlaywrightTimeoutError:
                        opened_pages.append((source, page, None))
                    except PlaywrightError:
                        results.append(BrowserSourceResult(source=source, listings=[], status="unavailable", error_type="navigation_failed"))

                if opened_pages:
                    opened_pages[0][1].bring_to_front()
                    sleep(BROWSER_REVIEW_SECONDS)

                for source, page, initial_status in opened_pages:
                    try:
                        html = page.content()
                        anchors = _visible_anchors(page)
                        listings = parse_browser_result_page(html, anchors, source)
                        if listings or initial_status is None or initial_status < 400:
                            results.append(BrowserSourceResult(source=source, listings=listings, status="ok" if listings else "empty"))
                        else:
                            results.append(BrowserSourceResult(source=source, listings=[], status="unavailable", error_type="http_error", http_status=initial_status))
                    except PlaywrightError:
                        results.append(BrowserSourceResult(source=source, listings=[], status="unavailable", error_type="extraction_failed"))
            finally:
                context.close()
    return results


def parse_browser_result_page(html: str, raw_anchors: list[dict[str, Any]], source: SourceURL) -> list[Listing]:
    """Normalize visible result-card links from an already loaded portal page."""
    html_listings = _html_listings(html, source)
    anchor_listings: list[Listing] = []
    for raw_anchor in raw_anchors[:MAX_BROWSER_LINKS]:
        try:
            anchor = BrowserAnchor.model_validate(raw_anchor)
        except ValidationError:
            continue
        listing = _listing_from_anchor(anchor, source)
        if listing is not None:
            anchor_listings.append(listing)
    merged = {listing.source_key: listing for listing in [*html_listings, *anchor_listings]}
    return list(merged.values())


@contextmanager
def _exclusive_profile_lock() -> Iterator[None]:
    import fcntl

    lock_path = PROFILE_DIRECTORY / ".search.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BrowserSearchBusyError("browser profile is busy") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _visible_anchors(page: Any) -> list[dict[str, Any]]:
    return page.locator("a[href]").evaluate_all(
        """elements => elements.slice(0, 350).map((element) => ({
          href: element.href,
          text: (element.innerText || "").slice(0, 1500),
        }))"""
    )


def _html_listings(html: str, source: SourceURL) -> list[Listing]:
    limited_html = html[:MAX_BROWSER_HTML_CHARS]
    if source.portal is Portal.ROOMIX:
        return parse_roomix_search_html(limited_html, source)
    return parse_json_ld(limited_html, source)


def _listing_from_anchor(anchor: BrowserAnchor, source: SourceURL) -> Listing | None:
    url = _public_property_url(anchor.href, source.portal)
    if url is None:
        return None
    text = " ".join(anchor.text.split())
    if len(text) < 4:
        return None
    price_match = _PRICE.search(text)
    title = text[: price_match.start()].strip(" ·|-–") if price_match else text
    title = title[:500].strip() or f"Aviso en {source.portal.value}"
    rooms_match = _ROOMS.search(text)
    bedrooms_match = _BEDROOMS.search(text)
    bathrooms_match = _BATHROOMS.search(text)
    area_match = _AREAS.search(text)
    try:
        return Listing(
            source=source.portal,
            source_url=source.url,
            url=url,
            title=title,
            address=title,
            price=_number_or_none(price_match.group("amount")) if price_match else None,
            currency=_currency_or_none(price_match.group("currency")) if price_match else None,
            rooms=1 if rooms_match and rooms_match.group("studio") else rooms_match.group("rooms") if rooms_match else None,
            bedrooms=bedrooms_match.group("bedrooms") if bedrooms_match else None,
            bathrooms=bathrooms_match.group("bathrooms") if bathrooms_match else None,
            covered_m2=_number_or_none(area_match.group("area")) if area_match else None,
            details=text,
        )
    except ValueError:
        return None


def _public_property_url(raw_url: str, portal: Portal) -> str | None:
    parsed = urlsplit(raw_url)
    clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    try:
        validate_portal_url(clean_url, portal)
    except ValueError:
        return None
    path = parsed.path.lower()
    if portal is Portal.ARGENPROP:
        return clean_url if "/propiedades/" in path or path.startswith("/departamento-en-") else None
    if portal is Portal.ZONAPROP:
        return clean_url if "/propiedades/" in path else None
    if portal is Portal.ROOMIX:
        return clean_url if path.startswith("/propiedad/") else None
    if portal is Portal.REMAX:
        return clean_url if path.startswith("/listings/") else None
    return clean_url if "/mla-" in path or path.startswith("/mla") else None


def _number_or_none(value: str) -> str | None:
    compact = value.replace(" ", "").replace(".", "").replace(",", ".")
    return compact if compact.replace(".", "", 1).isdigit() else None


def _currency_or_none(value: str) -> str:
    return "USD" if value.upper().replace("$", "") in {"USD", "US"} else "ARS"
