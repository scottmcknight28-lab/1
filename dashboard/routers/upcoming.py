from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from wine_agent.database import AuctionDatabase
from wine_agent.strategy import BiddingStrategy

router = APIRouter()


@router.get("/upcoming", include_in_schema=False)
async def upcoming_page(request: Request):
    db   = AuctionDatabase(request.app.state.db_path)
    cfg  = request.app.state.config_path
    strat = BiddingStrategy(cfg)

    open_lots = db.search_lots(current_only=True)
    recs = strat.bulk_evaluate(
        open_lots,
        get_market_avg=lambda n, v: db.get_market_average(n, v),
        min_score=0.0,          # show all scored lots so user can see scores
    )
    db.close()

    # Separate into recommended vs not-recommended for the template
    yes = [r for r in recs if r["bid"]]
    no  = [r for r in recs if not r["bid"]]

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
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        base_url = cfg.get("scraping", {}).get("base_url", "https://www.langtons.com.au")

        scraper = LangtonsScraper(base_url=base_url, delay_seconds=0.5)
        results: dict = {}

        # ── 1. Auction listing page ───────────────────────────────────
        for path in ["/auctions.html", "/auctions"]:
            url = base_url + path
            soup = scraper._fetch(url)
            if soup is None:
                results[path] = {"error": "fetch failed"}
                continue

            classes: set[str] = set()
            for el in soup.find_all(True):
                classes.update(el.get("class", []))

            tiles = soup.select(".auction-tile")
            first_tile_html = str(tiles[0])[:2000] if tiles else None
            auction_links = scraper._find_auction_links(soup)

            results[path] = {
                "html_length": len(str(soup)),
                "auction_tiles_found": len(tiles),
                "first_tile_html": first_tile_html,
                "auction_links_parsed": auction_links[:10],
                "css_classes_sample": sorted(classes)[:100],
            }

            # ── 2. Probe first auction's lot listing page ─────────────
            if auction_links:
                first = auction_links[0]
                lot_soup = scraper._fetch(first["url"])
                if lot_soup:
                    lot_classes: set[str] = set()
                    for el in lot_soup.find_all(True):
                        lot_classes.update(el.get("class", []))

                    lot_containers: list = []
                    matched_sel = None
                    for sel in _SELECTORS["lot_containers"]:
                        found = lot_soup.select(sel)
                        if found:
                            lot_containers = found
                            matched_sel = sel
                            break

                    first_lot_html = str(lot_containers[0])[:2000] if lot_containers else None

                    results["first_auction_lot_page"] = {
                        "url": first["url"],
                        "html_length": len(str(lot_soup)),
                        "matched_selector": matched_sel,
                        "lot_containers_found": len(lot_containers),
                        "first_lot_html": first_lot_html,
                        "css_classes_sample": sorted(lot_classes)[:100],
                    }
            break  # stop after first successful auction page

        return results

    result = await asyncio.to_thread(_debug, request.app.state.config_path)
    return result

