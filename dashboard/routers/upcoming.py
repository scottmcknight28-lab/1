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
    """Trigger an immediate scrape (runs synchronously – can be slow)."""
    from wine_agent.reporter import AuctionReporter
    reporter = AuctionReporter(request.app.state.config_path)
    auctions = reporter.scraper.get_auctions()
    saved = 0
    for auction in auctions[:5]:
        reporter.db.upsert_auction(auction)
        lots = reporter.scraper.get_lots(auction["url"], auction["auction_id"])
        for lot in lots:
            reporter.db.upsert_lot(lot)
        saved += len(lots)
    return {"status": "ok", "auctions": len(auctions), "lots_saved": saved}
