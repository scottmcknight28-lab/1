from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from wine_agent.database import AuctionDatabase
from wine_agent.strategy import BiddingStrategy

router = APIRouter()


@router.get("/upcoming", include_in_schema=False)
async def upcoming_page(request: Request):
    db    = AuctionDatabase(request.app.state.db_path)
    strat = BiddingStrategy(request.app.state.config_path)

    open_lots = db.search_lots(current_only=True)

    # Evaluate every open lot individually so we can show ALL of them,
    # not just the ones bulk_evaluate would filter (bid=True only).
    yes: list[dict] = []
    no:  list[dict] = []
    for lot in open_lots:
        avg = db.get_market_average(lot.get("wine_name", ""), lot.get("vintage"))
        ev  = strat.evaluate(lot, avg)
        rec = {
            "lot_id":        lot.get("id"),
            "lot_number":    lot.get("lot_number"),
            "wine_name":     lot.get("wine_name"),
            "vintage":       lot.get("vintage"),
            "producer":      lot.get("producer"),
            "region":        lot.get("region"),
            "estimate_low":  lot.get("estimate_low"),
            "estimate_high": lot.get("estimate_high"),
            "market_avg":    avg,
            **ev,
        }
        (yes if ev["bid"] else no).append(rec)

    yes.sort(key=lambda x: x["score"], reverse=True)
    no.sort(key=lambda x: x["score"], reverse=True)
    db.close()

    return request.app.state.templates.TemplateResponse(
        request, "upcoming.html",
        {"recommended": yes, "not_recommended": no, "open_count": len(open_lots)},
    )


@router.get("/api/recommendations")
async def api_recommendations(request: Request):
    db   = AuctionDatabase(request.app.state.db_path)
    recs = db.get_recommendations()
    db.close()
    return {"recommendations": recs, "count": len(recs)}


@router.post("/api/scrape")
async def api_scrape(request: Request):
    """Trigger an immediate scrape in a background thread (non-blocking)."""
    import asyncio
    from wine_agent.reporter import AuctionReporter

    def _do_scrape(config_path: str) -> dict:
        reporter = AuctionReporter(config_path)
        details = []
        errors  = []

        try:
            auctions = reporter.scraper.get_auctions()
        except Exception as exc:
            return {"status": "error", "message": f"Failed to fetch auctions: {exc}",
                    "auctions": 0, "lots_saved": 0}

        if not auctions:
            return {
                "status": "warning",
                "message": "No auctions found. The site may use JavaScript rendering — "
                           "check /api/scrape/debug for diagnostics.",
                "auctions": 0,
                "lots_saved": 0,
            }

        saved = 0
        for auction in auctions[:5]:
            reporter.db.upsert_auction(auction)
            try:
                lots = reporter.scraper.get_lots(auction["url"], auction["auction_id"])
                for lot in lots:
                    reporter.db.upsert_lot(lot)
                saved += len(lots)
                details.append({"auction_id": auction["auction_id"], "lots": len(lots)})
            except Exception as exc:
                errors.append(f"{auction['auction_id']}: {exc}")

        msg = f"Saved {saved} lots from {len(details)} auction(s)."
        if errors:
            msg += f" Errors: {'; '.join(errors[:2])}"

        return {"status": "ok", "message": msg, "auctions": len(auctions),
                "lots_saved": saved, "details": details}

    result = await asyncio.to_thread(_do_scrape, request.app.state.config_path)
    return result


@router.get("/api/scrape/debug")
async def api_scrape_debug(request: Request):
    """Fetch Langton's pages and return diagnostic info for tuning selectors."""
    import asyncio
    import yaml
    from wine_agent.scraper import LangtonsScraper, _SELECTORS

    def _debug(config_path: str) -> dict:
        import json, re as _re
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        base_url = cfg.get("scraping", {}).get("base_url", "https://www.langtons.com.au")

        scraper = LangtonsScraper(base_url=base_url, delay_seconds=0.5)
        results: dict = {}

        # ── 1. Probe key pages for auction discovery ──────────────────
        # Try several URL variants so we can see which one has the right structure
        probe_paths = [
            "/auctions.html",
            "/auctions?cgid=cat-l1-auctions&sz=24",
        ]
        for path in probe_paths:
            url = base_url + path if path.startswith("/") else path
            soup = scraper._fetch(url)
            if soup is None:
                results[path] = {"error": "fetch failed"}
                continue

            # Auction-tile slugs in the HTML (old-style /auctions/SLUG links)
            auction_slug_links = [
                a["href"] for a in soup.find_all("a", href=True)
                if _re.search(r"/auctions/[^/?#]+$", a["href"])
            ][:10]

            # Any href containing auctionId (JS-rendered refinements won't appear)
            auction_id_hrefs = [
                a["href"] for a in soup.find_all("a", href=True)
                if "auctionId" in a.get("href", "")
            ][:10]

            # Embedded JSON in <script> tags mentioning "auctionId" or "prefv"
            script_auction_snippets: list[str] = []
            for script in soup.find_all("script"):
                txt = script.string or ""
                if "auctionId" in txt or "prefv1" in txt or "cgid" in txt:
                    # Grab up to 500 chars around each hit
                    for m in _re.finditer(r"auctionId|prefv1", txt):
                        start = max(0, m.start() - 100)
                        script_auction_snippets.append(txt[start: m.start() + 200])
                        if len(script_auction_snippets) >= 5:
                            break
                if len(script_auction_snippets) >= 5:
                    break

            # Count auction-tile vs plain product-tile
            auction_tiles = soup.select("div.product-tile.auction-tile")
            product_tiles = soup.select("div.product-tile")

            results[path] = {
                "html_length":              len(str(soup)),
                "auction_tile_count":       len(auction_tiles),
                "product_tile_count":       len(product_tiles),
                "auction_slug_links":       auction_slug_links,
                "auction_id_hrefs":         auction_id_hrefs,
                "script_auction_snippets":  script_auction_snippets,
            }

            # ── 2. First auction's lot listing ────────────────────────
            # Use auction-tile slug links if found
            first_url = None
            if auction_slug_links:
                href = auction_slug_links[0]
                first_url = href if href.startswith("http") else base_url + href
            elif auction_tiles:
                a = auction_tiles[0].find("a", href=True)
                if a:
                    href = a["href"]
                    first_url = href if href.startswith("http") else base_url + href

            if not first_url:
                continue

            lot_soup = scraper._fetch(first_url)
            if lot_soup:
                lot_containers: list = []
                matched_sel = None
                for sel in _SELECTORS["lot_containers"]:
                    found = lot_soup.select(sel)
                    if found:
                        lot_containers = found
                        matched_sel = sel
                        break

                first_tile_full = str(lot_containers[0]) if lot_containers else None
                price_snippets: list[str] = []
                if lot_containers:
                    for el in lot_containers[0].find_all(True):
                        txt = el.get_text(" ", strip=True)
                        if "$" in txt and len(txt) < 100:
                            price_snippets.append(f"{el.name}.{' '.join(el.get('class',[]))}: {txt}")

                results["first_auction_lot_page"] = {
                    "url": first_url,
                    "html_length": len(str(lot_soup)),
                    "matched_selector": matched_sel,
                    "lot_containers_found": len(lot_containers),
                    "price_snippets_in_first_tile": price_snippets[:20],
                    "first_tile_full_html": first_tile_full,
                }
            break

        return results

    result = await asyncio.to_thread(_debug, request.app.state.config_path)
    return result

