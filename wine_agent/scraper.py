"""
Web scraper for Langton's Fine Wine Auctions (langtons.com.au).

The scraper is intentionally resilient: it tries multiple CSS selectors
so it can adapt as Langton's redesigns their pages.  Add new selectors
to the _SELECTORS map when you notice the site has changed.
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

# CSS selector candidates for common fields.  The scraper tries each in order
# and uses the first one that yields non-empty text.
_SELECTORS: dict[str, list[str]] = {
    "lot_number":   [".lot-number", ".lot-num", "[data-lot-number]", "h4"],
    "wine_name":    [".wine-name", ".lot-title h2", ".lot-title", "h1", "h2", "h3"],
    "producer":     [".producer", ".winery", "[class*='producer']", "[itemprop='brand']"],
    "region":       [".region", ".appellation", "[class*='region']", "[class*='appellation']"],
    "varietal":     [".varietal", ".grape", "[class*='varietal']", "[class*='grape']"],
    "estimate":     [".estimate", "[class*='estimate']", "[class*='price-estimate']"],
    "realized":     [".realized-price", ".hammer-price", "[class*='realized']", "[class*='hammer']"],
    "condition":    [".condition", ".provenance", "[class*='condition']", "[class*='provenance']"],
    "bottle_size":  [".bottle-size", "[class*='bottle-size']", "[class*='format']"],
    "quantity":     [".quantity", "[class*='quantity']", "[class*='bottles']"],
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
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-AU,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_auctions(self) -> list[dict]:
        """Return a list of auctions (current + recent past) from Langton's."""
        auctions: list[dict] = []
        seen: set[str] = set()

        for path in ["/auctions/", "/auctions/past/", "/auctions/results/"]:
            soup = self._fetch(self.base_url + path)
            if soup is None:
                continue
            for item in self._find_auction_links(soup):
                aid = item["auction_id"]
                if aid not in seen:
                    seen.add(aid)
                    auctions.append(item)

        return auctions

    def get_lots(self, auction_url: str, auction_id: str) -> list[dict]:
        """Scrape all lot cards from an auction listing page (handles pagination)."""
        lots: list[dict] = []
        page = 1

        while page <= self.max_pages:
            url = f"{auction_url}?page={page}" if page > 1 else auction_url
            soup = self._fetch(url)
            if soup is None:
                break

            page_lots = self._parse_lots_from_listing(soup, auction_id)
            if not page_lots:
                break

            lots.extend(page_lots)

            # Check for a "Next" pagination link
            if not self._has_next_page(soup):
                break
            page += 1

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
        # Match hrefs containing /auction/ or /auctions/ with an ID segment
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            m = re.search(r"/auctions?/([^/?#]+)", href)
            if not m:
                continue
            aid = m.group(1)
            # Skip obvious non-auction slugs
            if aid in ("past", "results", "current", "archive", "upcoming"):
                continue
            url = href if href.startswith("http") else self.base_url + href
            title = a.get_text(strip=True) or f"Auction {aid}"
            results.append(
                {
                    "auction_id": aid,
                    "title": title,
                    "auction_date": self._extract_date_from_text(title),
                    "url": url,
                    "scraped_at": datetime.now().isoformat(),
                }
            )
        return results

    # ------------------------------------------------------------------
    # Internal: Lot listing parsing
    # ------------------------------------------------------------------

    def _parse_lots_from_listing(self, soup: BeautifulSoup, auction_id: str) -> list[dict]:
        lots = []
        # Try a variety of containers that Langton's has used over time
        containers = soup.select(
            ".lot-item, .lot, [class*='lot-card'], "
            "[data-lot], article.wine, .wine-lot, .auction-lot"
        )
        if not containers:
            # Fallback: any <article> or <li> with a wine-looking link
            containers = [
                el for el in soup.find_all(["article", "li"])
                if el.find("a", href=re.compile(r"/lot[s]?/"))
            ]

        for el in containers:
            data = self._extract_lot_fields(el, auction_id)
            if data:
                lots.append(data)
        return lots

    def _extract_lot_fields(self, el: Tag, auction_id: str) -> Optional[dict]:
        wine_name = self._text(el, _SELECTORS["wine_name"])
        if not wine_name:
            return None

        lot_number  = self._text(el, _SELECTORS["lot_number"]) or ""
        producer    = self._text(el, _SELECTORS["producer"])
        region      = self._text(el, _SELECTORS["region"]) or ""
        varietal    = self._text(el, _SELECTORS["varietal"]) or ""
        vintage     = self._parse_vintage(wine_name)

        est_text    = self._text(el, _SELECTORS["estimate"])
        est_lo, est_hi = self._parse_estimate(est_text)

        real_text   = self._text(el, _SELECTORS["realized"])
        realized    = self._parse_price(real_text)

        cond_text   = self._text(el, _SELECTORS["condition"]) or ""
        fill        = self._parse_fill_level(cond_text)
        cellar      = 1 if re.search(r"cellar.?stor|professionally stor", cond_text, re.I) else 0
        oc          = 1 if re.search(r"\bOC\b|OWC|original.?carton", cond_text) else 0

        lot_url = ""
        link = el.find("a", href=True)
        if link:
            href = link["href"]
            lot_url = href if href.startswith("http") else self.base_url + href

        bsize_text  = self._text(el, _SELECTORS["bottle_size"]) or "750ml"
        qty_text    = self._text(el, _SELECTORS["quantity"]) or ""
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
            "bottle_size":     bsize_text,
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
    # Internal: Lot detail page parsing
    # ------------------------------------------------------------------

    def _parse_lot_detail(self, soup: BeautifulSoup, auction_id: str, lot_url: str) -> dict:
        wine_name   = self._text(soup, ["h1.lot-title", "h1", ".wine-title"]) or ""
        lot_number  = self._text(soup, _SELECTORS["lot_number"]) or ""
        producer    = self._text(soup, _SELECTORS["producer"])
        region      = self._text(soup, _SELECTORS["region"]) or ""
        varietal    = self._text(soup, _SELECTORS["varietal"]) or ""
        vintage     = self._parse_vintage(wine_name)

        est_text    = self._text(soup, _SELECTORS["estimate"])
        est_lo, est_hi = self._parse_estimate(est_text)

        real_text   = self._text(soup, _SELECTORS["realized"])
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
                "a[aria-label='Next'], a[aria-label='next page']"
            )
        )

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
        # "$200 - $400", "$200–400", "200 - 400"
        m = re.search(r"\$?([\d,]+)\s*[-–]\s*\$?([\d,]+)", text)
        if m:
            return float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        # Single value
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
        m = re.search(r"(\d+)\s*(?:bottle|x\s*\d|btl)", text, re.I)
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
        # Fall back to the first two words
        words = wine_name.split()
        return " ".join(words[:2]) if len(words) >= 2 else (words[0] if words else "")
