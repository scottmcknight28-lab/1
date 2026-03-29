from typing import Optional
from fastapi import APIRouter, Request, Query
from wine_agent.database import AuctionDatabase

router = APIRouter()


@router.get("/lots", include_in_schema=False)
async def lots_page(request: Request):
    db       = AuctionDatabase(request.app.state.db_path)
    auctions = db.list_auctions()
    db.close()
    return request.app.state.templates.TemplateResponse(
        request, "lots.html", {"auctions": auctions}
    )


@router.get("/api/lots")
async def api_lots(
    request:      Request,
    producer:     Optional[str] = Query(None),
    region:       Optional[str] = Query(None),
    wine_name:    Optional[str] = Query(None),
    min_vintage:  Optional[int] = Query(None),
    max_vintage:  Optional[int] = Query(None),
    auction_id:   Optional[str] = Query(None),
    current_only: bool          = Query(False),
):
    db   = AuctionDatabase(request.app.state.db_path)
    lots = db.search_lots(
        producer=producer, region=region, wine_name=wine_name,
        min_vintage=min_vintage, max_vintage=max_vintage,
        auction_id=auction_id, current_only=current_only,
    )
    db.close()
    return {"lots": lots, "count": len(lots)}


@router.get("/api/auctions")
async def api_auctions(request: Request):
    db       = AuctionDatabase(request.app.state.db_path)
    auctions = db.list_auctions()
    db.close()
    return {"auctions": auctions}
