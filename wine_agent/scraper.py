"""
Web scraper for Langton's Fine Wine Auctions (langtons.com.au).

The site runs on Salesforce Commerce Cloud (Demandware).
Key URL patterns discovered:
  - Auction listing:   /auctions.html
  - Closed results:    /on/demandware.store/Sites-langtons-Site/en_AU/
                         Auction-ClosedAuction?auctionStatus=Closed
  - Individual lots:   links found inside .auction-tile elements

CSS classes confirmed on the live site:
  auction-tile, bid-now, curr-bid, closing-soon, closing-later,
  classification-badge, classification-classified
"""

import logging
import re
import time
from datetime import datetime
from typing import Optional

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
    "featured-wine-brands", "australia", "france", "burgundy",
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

    def get_auctions(self) -> list[dict]:
        """Return a list of auctions from Langton's."""
        auctions: list[dict] = []
        seen: set[str] = set()

        # Primary: SFCC auction listing pages
        paths_to_try = [
            "/auctions.html",
            "/auctions",
            f"{_DW_BASE}/Auction-ClosedAuction?auctionStatus=Closed",
        ]
        for path in paths_to_try:
            soup = self._fetch(self.base_url + path)
            if soup is None:
                continue
            for item in self._find_auction_links(soup):
                aid = item["auction_id"]
                if aid not in seen:
                    seen.add(aid)
                    auctions.append(item)

        logger.info("Found %d auctions", len(auctions))
        return auctions

    def get_lots(self, auction_url: str, auction_id: str) -> list[dict]:
        """Scrape all lot cards from an auction listing page (handles pagination)."""
        lots: list[dict] = []
        page = 1

        while page <= self.max_pages:
            # SFCC pagination uses ?start=N&sz=N or ?page=N
            if page > 1:
                sep = "&" if "?" in auction_url else "?"
                url = f"{auction_url}{sep}page={page}&start={(page-1)*24}"
            else:
                url = auction_url

            soup = self._fetch(url)
            if soup is None:
                break

            page_lots = self._parse_lots_from_listing(soup, auction_id)
            if not page_lots:
                break

            lots.extend(page_lots)

            if not self._has_next_page(soup):
                break
            page += 1

        logger.info("Got %d lots from auction %s", len(lots), auction_id)
        return lots

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
        # .size-text gives "1 x Bottle" — parse both qty and format from it
        size_raw = self._text(el, _SELECTORS["bottle_size"]) or ""
        qty = self._parse_quantity(size_raw) or 1
        # Extract format word: "1 x Bottle" → "Bottle"
        m_fmt = re.search(r"[xX×]\s*(\w+)", size_raw)
        bsize_text = m_fmt.group(1).capitalize() if m_fmt else size_raw or "Bottle"

        # ── Closing date (open = no realized price yet) ───────────────
        countdown = el.select_one(".countdown[data-end-date]")
        closing_raw = countdown.get("data-end-date", "") if countdown else ""

        # ── Prices ───────────────────────────────────────────────────
        est_text   = self._text(el, _SELECTORS["estimate"])
        est_lo, est_hi = self._parse_estimate(est_text)

        real_text  = self._text(el, _SELECTORS["realized"])
        if not real_text:
            real_text = self._text(el, _SELECTORS["current_bid"])
        realized   = self._parse_price(real_text)

        # ── Condition / provenance ────────────────────────────────────
        cond_text = self._text(el, _SELECTORS["condition"]) or ""
        fill      = self._parse_fill_level(cond_text)
        cellar    = 1 if re.search(r"cellar.?stor|professionally stor", cond_text, re.I) else 0
        oc        = 1 if re.search(r"\bOC\b|OWC|original.?carton", cond_text) else 0

        return {
            "auction_id":      auction_id,
            "lot_number":      lot_number,
            "wine_name":       wine_name,
            "producer":        producer or self._infer_producer(wine_name),
            "vintage":         vintage,
            "region":          region,
            "varietal":        varietal,
            "bottle_count":    qty,
            "bottle_size":     bsize_text,
            "estimate_low":    est_lo,
            "estimate_high":   est_hi,
            "realized_price":  None,   # only set after auction closes; current bid ≠ realized
            "condition_notes": cond_text,
            "fill_level":      fill,
            "cellar_stored":   cellar,
            "original_carton": oc,
            "provenance":      cond_text,
            "lot_url":         lot_url,
            "closing_date":    closing_raw,
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

        return {
            "auction_id":      auction_id,
            "lot_number":      lot_number,
            "wine_name":       wine_name,
            "producer":        producer or self._infer_producer(wine_name),
            "vintage":         vintage,
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
        m = re.search(r"\$?([\d,]+)\s*[-–]\s*\$?([\d,]+)", text)
        if m:
            return float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        m = re.search(r"\$?([\d,]+)", text)
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
        m = re.search(r"(\d+)\s*[xX×]\s*\w", text)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s*(?:bottle|btl)", text, re.I)
        return int(m.group(1)) if m else None

    @staticmethod
    def _extract_date_from_text(text: str) -> str:
        m = re.search(
            r"\b(\d{1,2})[/\-\s](\w+)[/\-\s](\d{4})\b"
            r"|\b(\d{4})\b",
            text,
        )
        return m.group(0) if m else datetime.now().strftime("%Y-%m")

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
