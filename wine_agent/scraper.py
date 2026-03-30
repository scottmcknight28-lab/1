"""
Web scraper for Langton's Fine Wine Auctions (langtons.com.au).

The site runs on Salesforce Commerce Cloud (Demandware / SFCC).
Key URL patterns:
  - All auctions index:  /auctions?cgid=cat-l1-auctions&sz=60
  - Single auction lots: /auctions?cgid=cat-l1-auctions&prefn1=auctionId
                           &prefv1=<encoded_id>&sz=60&start=<N>
  - Closed results:      /on/demandware.store/Sites-langtons-Site/en_AU/
                           Auction-ClosedAuction?auctionStatus=Closed
  - Individual lot page: /p/<slug>/auc-var-<id>.html

Auctions are discovered from the auctionId SFCC refinement panel on the
index page: any <a> with prefn1=auctionId in its href is an auction.
"""

import logging
import re
import time
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse, unquote_plus

import requests
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

BASE_URL = "https://www.langtons.com.au"

# Demandware controller base — used for result pages
_DW_BASE = "/on/demandware.store/Sites-langtons-Site/en_AU"

# CSS selector candidates (tried in order; first non-empty match wins).
# Updated to match Langton's actual Salesforce Commerce Cloud template.
_SELECTORS: dict[str, list[str]] = {
    # Lot / product tile containers
    "lot_containers": [
        "div.product-tile.auction-tile",    # confirmed: both classes present
        ".product-tile",
        ".lot-tile", ".bid-tile",
        ".product-grid-item", "[class*='product-tile']",
        ".lot-item", ".lot", "[class*='lot-card']", "[data-lot]",
        "article.wine", ".wine-lot", ".auction-lot",
    ],
    "lot_number": [
        ".lot-number",                      # confirmed: "Lot # 90"
        ".lot-num", "[data-lot-number]",
        ".bid-number", ".tile-lot-number", "h4",
    ],
    "wine_name": [
        ".pdp-link .link",                  # confirmed: full wine name as text link
        ".product-name", ".tile-product-name", ".wine-name",
        ".pdp-name", ".lot-title h2", ".lot-title",
        "h1.product-name", "h1", "h2", "h3",
    ],
    "producer": [
        ".brand", ".producer", ".winery",
        "[class*='producer']", "[itemprop='brand']",
        ".tile-brand",
    ],
    "region": [
        ".region", ".appellation", ".tile-region",
        "[class*='region']", "[class*='appellation']",
    ],
    "varietal": [
        ".varietal", ".grape", ".tile-varietal",
        "[class*='varietal']", "[class*='grape']",
    ],
    "estimate": [
        ".price-estimate .price-info",      # confirmed: "Estimate Est/item $900.00 - $1300.00"
        ".price-info",
        ".estimate", ".price-estimate",
        "[class*='estimate']", "[class*='price-estimate']",
    ],
    "current_bid": [
        ".curr-bid .max-bid-price",         # confirmed: "$827.00"
        ".max-bid-price",
        ".curr-bid", ".current-bid",
        "[class*='curr-bid']", "[class*='current-bid']",
    ],
    "realized": [
        ".realized-price", ".hammer-price", ".sold-price",
        "div.max-bid-price.secondary-headline",  # confirmed on detail page
        ".max-bid-price",
        "[class*='realized']", "[class*='hammer']", "[class*='sold-price']",
    ],
    "condition": [
        "button.info-icon .tooltip",        # confirmed: "Base of Neck."
        "button.info-icon span",
        ".condition", ".provenance", ".classification-badge",
        "[class*='condition']", "[class*='provenance']",
    ],
    "bottle_size": [
        ".size-text",                       # confirmed: "1 x Bottle"
        ".bottle-size", ".format", ".tile-format",
        "[class*='bottle-size']", "[class*='format']",
    ],
    "quantity": [
        ".size-text",                       # "1 x Bottle" → parse count from this
        ".quantity", ".tile-quantity",
        "[class*='quantity']", "[class*='bottles']",
    ],
}

# Fill level hierarchy (best → worst fill)
FILL_LEVELS = [
    "into neck",
    "base of neck",
    "top shoulder",
    "upper shoulder",
    "mid shoulder",
    "lower shoulder",
]

# Slugs that appear in /auctions/ paths but are NOT individual auctions
_SKIP_SLUGS = {
    "past", "results", "current", "archive", "upcoming",
    "featured-wine-brands", "top-wine-regions",
    "australia", "france", "burgundy",
    "bordeaux", "champagne", "rhone", "italy",
}


class LangtonsScraper:
    def __init__(self, base_url: str = BASE_URL, delay_seconds: float = 2.0,
                 max_pages_per_auction: int = 10):
        self.base_url = base_url.rstrip("/")
        self.delay = delay_seconds
        self.max_pages = max_pages_per_auction or 999
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-AU,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": base_url,
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # Page size used for all SFCC auction listing requests
    _PAGE_SZ = 60

    def get_auctions(self) -> list[dict]:
        """Return a list of current auctions from Langton's.

        Discovery strategy:
        1. Slug-based featured pages (/auctions/top-wine-regions etc.)
        2. Current-week auction URLs derived from past auction_id_hrefs in the
           server-rendered HTML.  SFCC refinement links are JS-loaded, so we
           reverse-engineer them from the previous week's pattern links.
        3. Closed-auction endpoint for post-auction results.
        """
        auctions: list[dict] = []
        seen: set[str] = set()

        soup = self._fetch(self.base_url + "/auctions.html")
        if soup is not None:
            # Pattern-based fallback: derive current-week URLs from past patterns.
            # Reliable for auction series with consistent weekly names (e.g. TUE series).
            for item in self._generate_current_auction_urls(soup):
                if item["auction_id"] not in seen:
                    seen.add(item["auction_id"])
                    auctions.append(item)

        # 2. Live discovery: scan the main listing page for actual current auction IDs.
        # This catches weekly auctions whose names change (e.g. rotating SUN themes)
        # by finding real auction ID strings embedded in SFCC analytics/data attrs.
        for id_str in self._discover_live_auction_ids():
            item = self._auction_dict_from_id_str(id_str)
            if item["auction_id"] not in seen:
                seen.add(item["auction_id"])
                auctions.append(item)

        # 3. Closed-auction endpoint
        closed_url = self.base_url + f"{_DW_BASE}/Auction-ClosedAuction?auctionStatus=Closed"
        closed_soup = self._fetch(closed_url)
        if closed_soup:
            for item in self._find_auction_refinements(closed_soup):
                if item["auction_id"] not in seen:
                    seen.add(item["auction_id"])
                    auctions.append(item)

        logger.info("Found %d auctions", len(auctions))
        return auctions

    def get_lots(self, auction_url: str, auction_id: str) -> list[dict]:
        """Scrape all lot cards from an auction listing page (handles pagination).

        Langton's SFCC doesn't render pagination links server-side, so we keep
        fetching pages until a page returns fewer lots than _PAGE_SZ (indicating
        the last page) or we hit max_pages.
        """
        lots: list[dict] = []
        page = 1

        while page <= self.max_pages:
            url = self._page_url(auction_url, page)
            soup = self._fetch(url)
            if soup is None:
                break

            page_lots = self._parse_lots_from_listing(soup, auction_id)
            if not page_lots:
                break

            lots.extend(page_lots)

            # Stop when the page returned fewer results than a full page —
            # that means we've reached the end (works even without a "next" link).
            if len(page_lots) < self._PAGE_SZ:
                break
            page += 1

        logger.info("Got %d lots from auction %s", len(lots), auction_id)
        return lots

    def _page_url(self, base_url: str, page: int) -> str:
        """Return a SFCC-compatible paginated URL for the given 1-indexed page."""
        if page == 1:
            return base_url
        start = (page - 1) * self._PAGE_SZ
        parsed = urlparse(base_url)
        params = dict(parse_qsl(parsed.query))
        params["start"] = str(start)
        params["sz"] = str(self._PAGE_SZ)
        return urlunparse(parsed._replace(query=urlencode(params)))

    def get_lot_detail(self, lot_url: str, auction_id: str) -> Optional[dict]:
        """Scrape the detail page for a single lot (richer condition/provenance data)."""
        soup = self._fetch(lot_url)
        if soup is None:
            return None
        return self._parse_lot_detail(soup, auction_id, lot_url)

    # ------------------------------------------------------------------
    # Internal: HTTP
    # ------------------------------------------------------------------

    def _fetch(self, url: str) -> Optional[BeautifulSoup]:
        time.sleep(self.delay)
        try:
            resp = self.session.get(url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as exc:
            logger.warning("Failed to fetch %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # Internal: Auction list parsing
    # ------------------------------------------------------------------

    def _find_auction_refinements(self, soup: BeautifulSoup) -> list[dict]:
        """
        Parse SFCC auctionId refinement links to discover all live auctions.

        Any <a> whose href contains prefn1=auctionId (or prefn2= etc.) and a
        corresponding prefv value is an individual auction filter link.
        Example href: /auctions?cgid=cat-l1-auctions&prefn1=auctionId
                        &prefv1=2026-03-31%3B+31+TUE%3A+Australian+Cellar+Selection&sz=24
        """
        results: list[dict] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            # Look for any SFCC refinement link that filters by auctionId
            if "auctionId" not in href:
                continue
            m = re.search(r"prefv\d+=([^&]+)", href)
            if not m:
                continue
            raw_value = unquote_plus(m.group(1))
            if not raw_value or raw_value in seen:
                continue
            seen.add(raw_value)

            # Build the canonical lot-listing URL for this auction
            url = (href if href.startswith("http") else self.base_url + href)
            # Ensure sz is set to our preferred page size
            parsed = urlparse(url)
            params = dict(parse_qsl(parsed.query))
            params["sz"] = str(self._PAGE_SZ)
            params.pop("start", None)
            url = urlunparse(parsed._replace(query=urlencode(params)))

            # Use the raw prefv value as auction_id (stable, human-readable)
            auction_id = re.sub(r"[^a-z0-9-]", "-", raw_value.lower())[:60].strip("-")
            title = a.get_text(strip=True) or raw_value

            results.append({
                "auction_id":   auction_id,
                "title":        title,
                "auction_date": self._extract_date_from_text(raw_value),
                "url":          url,
                "scraped_at":   datetime.now().isoformat(),
            })

        return results

    def _discover_live_auction_ids(self) -> list[str]:
        """
        Fetch the main live auction listing and scan ALL HTML (including scripts
        and data attributes) for Langton's auction ID strings of the form:
          "YYYY-MM-DD; DD DAY: Auction Name"

        SFCC often embeds these in analytics dataLayer pushes, data-* attributes,
        and URL parameters within the rendered tile HTML.
        """
        soup = self._fetch(f"{self.base_url}/auctions?cgid=cat-l1-auctions&sz=60")
        if soup is None:
            return []

        # Pattern matches the full auction ID string in any context
        # Allow for URL-encoded variants (%3B = ;, %3A = :) and handle both
        id_re = re.compile(
            r"\d{4}-\d{2}-\d{2}[;%][\s+]?\d+[\s+]+[A-Z]{2,3}[:%][\s+]"
            r"[A-Za-z0-9 &',\-\u2018\u2019\u201c\u201d%+]+"
        )

        found: set[str] = set()

        # Scan the raw serialised HTML — catches dataLayer JSON, data-attrs, hrefs
        raw = str(soup)
        # First pass: URL-decode the whole blob so %3B → ; and %3A → :
        decoded_raw = unquote_plus(raw)
        for m in id_re.finditer(decoded_raw):
            candidate = m.group(0).strip().rstrip("\\\"' ")
            # Normalise whitespace
            candidate = re.sub(r"\s+", " ", candidate)
            # Must look like a real auction ID
            if re.search(r"\d{4}-\d{2}-\d{2};\s*\d+\s+[A-Z]{2,3}:\s*\S", candidate):
                found.add(candidate)

        logger.info("Live auction scan found %d auction IDs", len(found))
        return list(found)

    def _auction_dict_from_id_str(self, auction_id_str: str) -> dict:
        """Build an auction discovery dict from a raw SFCC auction ID string."""
        auction_id = re.sub(r"[^a-z0-9-]", "-", auction_id_str.lower())[:60].strip("-")
        url = (
            f"{self.base_url}/auctions"
            f"?cgid=cat-l1-auctions"
            f"&prefn1=auctionId"
            f"&prefv1={quote_plus(auction_id_str)}"
            f"&sz={self._PAGE_SZ}"
        )
        # Extract a clean title (strip the date prefix, keep "DD DAY: Name")
        m = re.match(r"\d{4}-\d{2}-\d{2};\s*(.+)", auction_id_str)
        title = m.group(1).strip() if m else auction_id_str
        date_m = re.match(r"(\d{4}-\d{2}-\d{2})", auction_id_str)
        return {
            "auction_id":   auction_id,
            "title":        title,
            "auction_date": date_m.group(1) if date_m else "",
            "url":          url,
            "scraped_at":   datetime.now().isoformat(),
        }

    def _generate_current_auction_urls(self, soup: BeautifulSoup) -> list[dict]:
        """
        Generate current-week auction lot-listing URLs.

        SFCC renders the auctionId refinement panel via JavaScript, so the live
        filter links are not in the server HTML.  However, the page *does* contain
        server-rendered links to last week's same-day auctions, formatted as:

          /auctions?…&prefv1=YYYY-MM-DD%3B+DD+DAY%3A+Name|…&srule=…

        We parse those to extract (day_abbr, name) patterns, then build the
        equivalent URLs for the upcoming occurrence of each weekday.
        """
        from datetime import date, timedelta

        today = date.today()

        _DAY_TO_WEEKDAY = {
            "MON": 0, "TUE": 1, "WED": 2, "THU": 3,
            "FRI": 4, "SAT": 5, "SUN": 6,
        }

        # Collect unique (day_abbr, name) pairs from all pipe-separated prefv1 values
        seen_pairs: set[tuple[str, str]] = set()
        auction_types: list[tuple[str, str]] = []

        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if "auctionId" not in href:
                continue
            m = re.search(r"prefv\d+=([^&]+)", href)
            if not m:
                continue
            raw_prefv = unquote_plus(m.group(1))
            for part in raw_prefv.split("|"):
                part = part.strip()
                # Expected format: "YYYY-MM-DD; DD DAY: Auction Name"
                m2 = re.match(r"\d{4}-\d{2}-\d{2};\s*\d+\s+(\w{2,3}):\s*(.+)", part)
                if not m2:
                    continue
                day_abbr = m2.group(1).upper()
                name = m2.group(2).strip()
                if day_abbr not in _DAY_TO_WEEKDAY:
                    continue
                key = (day_abbr, name)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    auction_types.append(key)

        if not auction_types:
            logger.debug("No past auction_id_hrefs found; weekly auction discovery skipped")
            return []

        def upcoming_date_for(day_abbr: str) -> date:
            """Return the next occurrence of day_abbr's weekday, including today."""
            target_wd = _DAY_TO_WEEKDAY[day_abbr]
            days_ahead = (target_wd - today.weekday()) % 7
            return today + timedelta(days=days_ahead)

        results: list[dict] = []
        seen_ids: set[str] = set()

        for day_abbr, name in auction_types:
            auction_date = upcoming_date_for(day_abbr)
            day_num  = auction_date.day
            date_str = auction_date.strftime("%Y-%m-%d")

            # Reconstruct the exact SFCC auction ID string
            auction_id_str = f"{date_str}; {day_num} {day_abbr}: {name}"

            # Slugified key for our database (stable, human-readable)
            auction_id = re.sub(r"[^a-z0-9-]", "-", auction_id_str.lower())[:60].strip("-")

            if auction_id in seen_ids:
                continue
            seen_ids.add(auction_id)

            url = (
                f"{self.base_url}/auctions"
                f"?cgid=cat-l1-auctions"
                f"&prefn1=auctionId"
                f"&prefv1={quote_plus(auction_id_str)}"
                f"&sz={self._PAGE_SZ}"
            )

            results.append({
                "auction_id":   auction_id,
                "title":        f"{day_num} {day_abbr}: {name}",
                "auction_date": date_str,
                "url":          url,
                "scraped_at":   datetime.now().isoformat(),
            })

        logger.info("Generated %d current-week auction URLs from past patterns", len(results))
        return results

    def _find_auction_links(self, soup: BeautifulSoup) -> list[dict]:
        results: list[dict] = []

        # Strategy 1: Langton's auction-tile elements (confirmed present)
        tiles = soup.select(".auction-tile")
        if tiles:
            for tile in tiles:
                item = self._auction_from_tile(tile)
                if item:
                    results.append(item)
            if results:
                return results

        # Strategy 2: Any <a> whose href looks like an individual auction
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            # Match /auctions/SLUG or /auction/SLUG (no sub-path)
            m = re.match(r"^(?:https?://[^/]+)?/auctions?/([^/?#]+)$", href)
            if not m:
                continue
            slug = m.group(1)
            if slug in _SKIP_SLUGS:
                continue
            url = href if href.startswith("http") else self.base_url + href
            title = a.get_text(strip=True) or f"Auction {slug}"
            results.append({
                "auction_id": slug,
                "title":      title,
                "auction_date": self._extract_date_from_text(title),
                "url":         url,
                "scraped_at":  datetime.now().isoformat(),
            })

        return results

    def _auction_from_tile(self, tile: Tag) -> Optional[dict]:
        link = tile.find("a", href=True)
        if not link:
            return None
        href: str = link["href"]
        url = href if href.startswith("http") else self.base_url + href

        # Derive a stable auction_id from the URL
        m = re.search(r"/auctions?/([^/?#]+)", href)
        if m:
            slug = m.group(1)
        else:
            # Use last path segment or query param auctionID
            qm = re.search(r"auctionID=([^&]+)", href)
            slug = qm.group(1) if qm else re.sub(r"[^a-z0-9-]", "", href.split("/")[-1].lower())[:40]

        if not slug or slug in _SKIP_SLUGS:
            return None

        # Title: try heading elements first, then link text
        title_el = tile.select_one(
            ".auction-name, .tile-title, .auction-title, h1, h2, h3, h4, .name"
        )
        title = (title_el.get_text(strip=True) if title_el
                 else link.get_text(strip=True) or f"Auction {slug}")

        return {
            "auction_id":   slug,
            "title":        title,
            "auction_date": self._extract_date_from_text(title),
            "url":          url,
            "scraped_at":   datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Internal: Lot listing parsing
    # ------------------------------------------------------------------

    def _parse_lots_from_listing(self, soup: BeautifulSoup, auction_id: str) -> list[dict]:
        lots = []

        # Try selectors in priority order
        containers = []
        for sel in _SELECTORS["lot_containers"]:
            containers = soup.select(sel)
            if containers:
                logger.debug("Matched lot containers with selector: %s (%d found)", sel, len(containers))
                break

        # Last resort: any element containing a curr-bid span
        if not containers:
            containers = [el.parent for el in soup.select(".curr-bid") if el.parent]

        # Last resort 2: li/article with lot-looking links
        if not containers:
            containers = [
                el for el in soup.find_all(["article", "li"])
                if el.find("a", href=re.compile(r"/lot[s]?/", re.I))
            ]

        for el in containers:
            data = self._extract_lot_fields(el, auction_id)
            if data:
                lots.append(data)
        return lots

    def _extract_lot_fields(self, el: Tag, auction_id: str) -> Optional[dict]:
        # ── Wine name ─────────────────────────────────────────────────
        # Langton's tiles: wine name lives in img.tile-image[alt], e.g.
        # "CHATEAU LATOUR 1er cru classe, Pauillac 2008 Bottle"
        wine_name = self._text(el, _SELECTORS["wine_name"])
        if not wine_name:
            img = el.select_one("img.tile-image, a.js-pdp-link img, a.pdp-link-anchor img")
            if img:
                wine_name = img.get("alt", "").strip()
        if not wine_name:
            return None

        # ── Lot URL & number ──────────────────────────────────────────
        # PDP link: /p/wine-slug/auc-var-26869507.html
        lot_url = ""
        pdp = el.select_one("a.js-pdp-link, a.pdp-link-anchor, a[class*='pdp-link']")
        if pdp:
            href = pdp.get("href", "")
            lot_url = href if href.startswith("http") else self.base_url + href
        else:
            link = el.find("a", href=True)
            if link:
                href = link["href"]
                lot_url = href if href.startswith("http") else self.base_url + href

        # Lot number — ".lot-number" gives "Lot # 90", strip prefix
        lot_number_raw = self._text(el, _SELECTORS["lot_number"]) or ""
        m_lot = re.search(r"\d+", lot_number_raw)
        lot_number = m_lot.group(0) if m_lot else ""
        if not lot_number:
            # Fallback: extract from auc-var-NNNNNN in URL
            m = re.search(r"auc-var-(\d+)", lot_url)
            if m:
                lot_number = m.group(1)

        # ── Parse the Langton's naming convention ────────────────────
        # raw wine_name = "PRODUCER label, Region YYYY Format" (from img alt)
        parsed_producer, wine_label, varietal, parsed_region = \
            self._parse_langtons_name(wine_name)

        # Store a clean, title-cased wine_name (producer + label, no region/vintage)
        if parsed_producer and wine_label:
            wine_name = f"{parsed_producer} {wine_label}"
        elif wine_label:
            wine_name = wine_label

        producer = (self._text(el, _SELECTORS["producer"]) or parsed_producer
                    or self._infer_producer(wine_name))
        region   = self._text(el, _SELECTORS["region"]) or parsed_region or ""
        varietal = self._text(el, _SELECTORS["varietal"]) or varietal or ""
        vintage  = self._parse_vintage(wine_name) or self._parse_vintage(wine_label)

        # ── Bottle format & quantity ──────────────────────────────────
        # .size-text gives "1 x Bottle" — extract qty and format in one pass
        size_raw = self._text(el, _SELECTORS["bottle_size"]) or ""
        m_size = re.search(r"(\d+)\s*[xX×]\s*(\w+)", size_raw)
        if m_size:
            qty = int(m_size.group(1))
            bsize_text = m_size.group(2).capitalize()
        else:
            qty = self._parse_quantity(size_raw) or 1
            bsize_text = size_raw or "Bottle"

        # ── Closing date (open = no realized price yet) ───────────────
        countdown = el.select_one(".countdown[data-end-date]")
        closing_raw = countdown.get("data-end-date", "") if countdown else ""

        # ── Prices ───────────────────────────────────────────────────
        est_text = self._text(el, _SELECTORS["estimate"])
        est_lo, est_hi = self._parse_estimate(est_text)

        # Current bid — live price during auction; NOT the realized/hammer price
        current_bid = self._parse_price(self._text(el, _SELECTORS["current_bid"]) or "")

        # ── Critic scores ─────────────────────────────────────────────
        scores = []
        for badge in el.select(".critic-badge"):
            score_el    = badge.select_one(".critic-review-score")
            initials_el = badge.select_one(".critic-initials")
            if score_el and initials_el:
                scores.append(
                    f"{score_el.get_text(strip=True)} {initials_el.get_text(strip=True)}"
                )
        critic_scores = ", ".join(scores) if scores else None

        # ── Condition / provenance ────────────────────────────────────
        cond_text = self._text(el, _SELECTORS["condition"]) or ""
        fill      = self._parse_fill_level(cond_text)
        cellar    = 1 if re.search(r"cellar.?stor|professionally stor", cond_text, re.I) else 0
        oc        = 1 if re.search(r"\bOC\b|OWC|original.?carton", cond_text) else 0

        country = self._infer_country(region, wine_name)

        return {
            "auction_id":      auction_id,
            "lot_number":      lot_number,
            "wine_name":       wine_name,
            "producer":        producer or self._infer_producer(wine_name),
            "vintage":         vintage,
            "country":         country,
            "region":          region,
            "varietal":        varietal,
            "bottle_count":    qty,
            "bottle_size":     bsize_text,
            "estimate_low":    est_lo,
            "estimate_high":   est_hi,
            "realized_price":  None,   # populated post-auction via detail-page fetch
            "current_bid":     current_bid,
            "closing_date":    closing_raw,
            "critic_scores":   critic_scores,
            "condition_notes": cond_text,
            "fill_level":      fill,
            "cellar_stored":   cellar,
            "original_carton": oc,
            "provenance":      cond_text,
            "lot_url":         lot_url,
        }

    # ------------------------------------------------------------------
    # Internal: Lot detail page parsing
    # ------------------------------------------------------------------

    def _parse_lot_detail(self, soup: BeautifulSoup, auction_id: str, lot_url: str) -> dict:
        wine_name   = self._text(soup, ["h1.product-name", "h1.lot-title", "h1", ".wine-title"]) or ""
        # Fallback: page title's og:title meta
        if not wine_name:
            og = soup.find("meta", property="og:title")
            if og:
                wine_name = og.get("content", "").strip()

        lot_number  = self._text(soup, _SELECTORS["lot_number"]) or ""
        if not lot_number:
            m = re.search(r"auc-var-(\d+)", lot_url)
            if m:
                lot_number = m.group(1)

        parsed_prod, wine_label, varietal_parsed, parsed_region = \
            self._parse_langtons_name(wine_name)
        if parsed_prod and wine_label:
            wine_name = f"{parsed_prod} {wine_label}"

        producer = self._text(soup, _SELECTORS["producer"]) or parsed_prod or ""
        region   = self._text(soup, _SELECTORS["region"])   or parsed_region or ""
        varietal = self._text(soup, _SELECTORS["varietal"]) or varietal_parsed or ""
        vintage  = self._parse_vintage(wine_name)

        est_text    = self._text(soup, _SELECTORS["estimate"])
        est_lo, est_hi = self._parse_estimate(est_text)

        real_text   = self._text(soup, _SELECTORS["realized"]) or self._text(soup, _SELECTORS["current_bid"])
        realized    = self._parse_price(real_text)

        cond_text   = self._text(soup, _SELECTORS["condition"]) or ""
        fill        = self._parse_fill_level(cond_text)
        cellar      = 1 if re.search(r"cellar.?stor|professionally stor", cond_text, re.I) else 0
        oc          = 1 if re.search(r"\bOC\b|OWC|original.?carton", cond_text) else 0

        bsize       = self._text(soup, _SELECTORS["bottle_size"]) or "750ml"
        qty_text    = self._text(soup, _SELECTORS["quantity"]) or ""
        qty = self._parse_quantity(qty_text) or 1

        country = self._infer_country(region, wine_name)

        return {
            "auction_id":      auction_id,
            "lot_number":      lot_number,
            "wine_name":       wine_name,
            "producer":        producer or self._infer_producer(wine_name),
            "vintage":         vintage,
            "country":         country,
            "region":          region,
            "varietal":        varietal,
            "bottle_count":    qty,
            "bottle_size":     bsize,
            "estimate_low":    est_lo,
            "estimate_high":   est_hi,
            "realized_price":  realized,
            "condition_notes": cond_text,
            "fill_level":      fill,
            "cellar_stored":   cellar,
            "original_carton": oc,
            "provenance":      cond_text,
            "lot_url":         lot_url,
        }

    # ------------------------------------------------------------------
    # Internal: Helpers
    # ------------------------------------------------------------------

    def _text(self, el: Tag, selectors: list[str]) -> Optional[str]:
        for sel in selectors:
            found = el.select_one(sel)
            if found:
                t = found.get_text(" ", strip=True)
                if t:
                    return t
        return None

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        return bool(
            soup.select_one(
                "a[rel='next'], .pagination .next, "
                "a[aria-label='Next'], a[aria-label='next page'], "
                ".show-more, [class*='load-more']"
            )
        )

    @staticmethod
    def _parse_langtons_name(full_name: str) -> tuple[str, str, str, str]:
        """
        Parse Langton's wine name convention into (producer, wine_label, varietal, region).

        Langton's format: "ALL_CAPS_PRODUCER mixed case label, Region YYYY Format"

        Examples:
          "PENFOLDS Bin 820 Cabernet Shiraz, Coonawarra 2019 Bottle"
            -> ("Penfolds", "Bin 820 Cabernet Shiraz", "Cabernet Shiraz", "Coonawarra")
          "CHATEAU LATOUR 1er cru classe, Pauillac 2008 Bottle"
            -> ("Chateau Latour", "1er cru classe", "", "Pauillac")
          "GIACONDA Chardonnay, Beechworth 2021 Bottle"
            -> ("Giaconda", "Chardonnay", "Chardonnay", "Beechworth")
          "HENSCHKE Hill of Grace, Eden Valley 2020 Bottle"
            -> ("Henschke", "Hill of Grace", "", "Eden Valley")
        """
        # ── 1. Split region off after the comma ───────────────────────
        if "," in full_name:
            name_part, region_part = full_name.split(",", 1)
            m = re.match(r"\s*([A-Za-z][A-Za-z\s\-\'\.]*?)(?:\s+\d{4}|\s*$)", region_part)
            region = m.group(1).strip() if m else ""
        else:
            name_part = full_name
            region = ""

        # ── 2. Identify ALL-CAPS producer prefix ──────────────────────
        # A "producer word" is any token whose alpha characters are all uppercase.
        words = name_part.strip().split()
        producer_words: list[str] = []
        label_words: list[str] = []
        in_producer = True
        for word in words:
            alpha = re.sub(r"[^A-Za-z]", "", word)
            if in_producer and alpha and alpha.isupper() and len(alpha) >= 2:
                producer_words.append(word)
            else:
                in_producer = False
                label_words.append(word)

        producer   = " ".join(producer_words).title() if producer_words else ""
        wine_label = " ".join(label_words)

        # ── 3. Extract varietal from the wine label ───────────────────
        # Ordered longest-first so "Cabernet Sauvignon" beats "Cabernet".
        _VARIETALS = [
            "Cabernet Sauvignon", "Cabernet Shiraz", "Cabernet Merlot",
            "Shiraz Viognier", "Pinot Noir", "Sauvignon Blanc",
            "Pinot Gris", "Pinot Grigio",
            "Shiraz", "Chardonnay", "Riesling", "Semillon", "Sémillon",
            "Merlot", "Grenache", "Tempranillo", "Verdelho", "Viognier",
            "Cabernet", "Syrah",
        ]
        varietal = ""
        label_lower = wine_label.lower()
        for v in _VARIETALS:
            if v.lower() in label_lower:
                varietal = v
                break

        return producer, wine_label, varietal, region

    @staticmethod
    def _parse_vintage(text: Optional[str]) -> Optional[int]:
        if not text:
            return None
        m = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", text)
        return int(m.group(1)) if m else None

    @staticmethod
    def _parse_estimate(text: Optional[str]) -> tuple[Optional[float], Optional[float]]:
        if not text:
            return None, None
        # Match price ranges like "$4,200.00 - $5,300.00" or "$900 – $1,300"
        m = re.search(r"\$?([\d,]+(?:\.\d+)?)\s*[-–]\s*\$?([\d,]+(?:\.\d+)?)", text)
        if m:
            return float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        m = re.search(r"\$?([\d,]+(?:\.\d+)?)", text)
        if m:
            v = float(m.group(1).replace(",", ""))
            return v, v
        return None, None

    @staticmethod
    def _parse_price(text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        m = re.search(r"\$?([\d,]+)", text)
        return float(m.group(1).replace(",", "")) if m else None

    @staticmethod
    def _parse_fill_level(text: str) -> str:
        tl = text.lower()
        for level in FILL_LEVELS:
            if level in tl:
                return level
        return ""

    @staticmethod
    def _parse_quantity(text: str) -> Optional[int]:
        # Handles: "1 x Bottle", "6 x Bottle", "12 bottles", "6btl"
        m = re.search(r"(\d+)\s*(?:[xX×]\s*\w|bottle|btl)", text, re.I)
        return int(m.group(1)) if m else None

    @staticmethod
    def _extract_date_from_text(text: str) -> str:
        m = re.search(
            r"\b(\d{1,2})[/\-\s](\w+)[/\-\s](\d{4})\b"
            r"|\b(\d{4})\b",
            text,
        )
        return m.group(0) if m else datetime.now().strftime("%Y-%m")

    # Region keyword → country (checked case-insensitively; longer strings first)
    _REGION_COUNTRY: list[tuple[str, str]] = [
        # Australia
        ("barossa",         "Australia"), ("mclaren vale",     "Australia"),
        ("coonawarra",      "Australia"), ("clare valley",     "Australia"),
        ("eden valley",     "Australia"), ("margaret river",   "Australia"),
        ("yarra valley",    "Australia"), ("mornington",       "Australia"),
        ("hunter valley",   "Australia"), ("heathcote",        "Australia"),
        ("beechworth",      "Australia"), ("grampians",        "Australia"),
        ("pyrenees",        "Australia"), ("mudgee",           "Australia"),
        ("rutherglen",      "Australia"), ("great southern",   "Australia"),
        ("swan valley",     "Australia"), ("padthaway",        "Australia"),
        ("south australia", "Australia"), ("western australia","Australia"),
        ("victoria",        "Australia"), ("new south wales",  "Australia"),
        ("tasmania",        "Australia"),
        # France
        ("pauillac",        "France"),    ("margaux",          "France"),
        ("saint-julien",    "France"),    ("st julien",        "France"),
        ("saint-estèphe",   "France"),    ("st estephe",       "France"),
        ("pomerol",         "France"),    ("saint-emilion",    "France"),
        ("st emilion",      "France"),    ("sauternes",        "France"),
        ("pessac",          "France"),    ("graves",           "France"),
        ("médoc",           "France"),    ("medoc",            "France"),
        ("chambolle",       "France"),    ("gevrey",           "France"),
        ("vosne",           "France"),    ("nuits",            "France"),
        ("volnay",          "France"),    ("pommard",          "France"),
        ("meursault",       "France"),    ("puligny",          "France"),
        ("chassagne",       "France"),    ("montrachet",       "France"),
        ("chablis",         "France"),    ("macon",            "France"),
        ("beaujolais",      "France"),    ("rhône",            "France"),
        ("rhone",           "France"),    ("hermitage",        "France"),
        ("châteauneuf",     "France"),    ("chateauneuf",      "France"),
        ("gigondas",        "France"),    ("burgundy",         "France"),
        ("bordeaux",        "France"),    ("champagne",        "France"),
        ("alsace",          "France"),    ("loire",            "France"),
        ("sancerre",        "France"),    ("pouilly",          "France"),
        ("muscadet",        "France"),    ("provence",         "France"),
        ("languedoc",       "France"),    ("roussillon",       "France"),
        ("côtes du rhône",  "France"),    ("échezeaux",        "France"),
        ("echezeaux",       "France"),    ("romanée",          "France"),
        ("romanee",         "France"),    ("corton",           "France"),
        ("bâtard",          "France"),    ("batard",           "France"),
        # Italy
        ("barolo",          "Italy"),     ("barbaresco",       "Italy"),
        ("brunello",        "Italy"),     ("montalcino",       "Italy"),
        ("chianti",         "Italy"),     ("tuscany",          "Italy"),
        ("toscana",         "Italy"),     ("piedmont",         "Italy"),
        ("piemonte",        "Italy"),     ("veneto",           "Italy"),
        ("amarone",         "Italy"),     ("soave",            "Italy"),
        ("sicily",          "Italy"),     ("sardinia",         "Italy"),
        # USA
        ("napa valley",     "USA"),       ("napa",             "USA"),
        ("sonoma",          "USA"),       ("santa barbara",    "USA"),
        ("santa cruz",      "USA"),       ("paso robles",      "USA"),
        ("willamette",      "USA"),       ("oregon",           "USA"),
        ("washington state","USA"),
        # Spain
        ("rioja",           "Spain"),     ("ribera del duero", "Spain"),
        ("priorat",         "Spain"),     ("rias baixas",      "Spain"),
        ("penedès",         "Spain"),
        # Germany
        ("mosel",           "Germany"),   ("rheingau",         "Germany"),
        ("pfalz",           "Germany"),   ("rheinhessen",      "Germany"),
        ("nahe",            "Germany"),
        # New Zealand
        ("marlborough",     "New Zealand"), ("central otago",  "New Zealand"),
        ("hawke's bay",     "New Zealand"), ("hawkes bay",     "New Zealand"),
        ("martinborough",   "New Zealand"),
        # Portugal
        ("douro",           "Portugal"),  ("port",             "Portugal"),
        ("alentejo",        "Portugal"),  ("vinho verde",      "Portugal"),
        # Austria
        ("wachau",          "Austria"),   ("kamptal",          "Austria"),
        ("styria",          "Austria"),
        # Argentina
        ("mendoza",         "Argentina"), ("malbec argentina", "Argentina"),
        # Chile
        ("maipo",           "Chile"),     ("colchagua",        "Chile"),
        ("casablanca",      "Chile"),
    ]

    @classmethod
    def _infer_country(cls, region: str, wine_name: str) -> str:
        """Infer country from region text or wine name using keyword matching."""
        text = f"{region} {wine_name}".lower()
        for keyword, country in cls._REGION_COUNTRY:
            if keyword in text:
                return country
        return ""

    # Known Australian fine wine producers for name inference
    _KNOWN_PRODUCERS = [
        "Penfolds", "Henschke", "Leeuwin", "Giaconda", "Clarendon Hills",
        "Torbreck", "Two Hands", "Rockford", "Chris Ringland", "Greenock Creek",
        "Wendouree", "Moss Wood", "Cullen", "Cape Mentelle", "Vasse Felix",
        "Tyrrell's", "Brokenwood", "McWilliam's", "De Bortoli", "Yering Station",
        "Bass Phillip", "Bindi", "Mount Mary", "Tolpuddle", "Domaine Lucci",
    ]

    def _infer_producer(self, wine_name: str) -> str:
        for p in self._KNOWN_PRODUCERS:
            if p.lower() in wine_name.lower():
                return p
        words = wine_name.split()
        return " ".join(words[:2]) if len(words) >= 2 else (words[0] if words else "")
