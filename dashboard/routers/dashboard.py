from fastapi import APIRouter, Request
from wine_agent.database import AuctionDatabase
from dashboard.scheduler_bridge import get_job_schedule

router = APIRouter()


@router.get("/", include_in_schema=False)
async def index(request: Request):
    db    = AuctionDatabase(request.app.state.db_path)
    stats = db.get_stats()
    recs  = db.get_recommendations()[:5]
    db.close()

    jobs = get_job_schedule(request.app.state.scheduler)

    return request.app.state.templates.TemplateResponse(
        request, "dashboard.html",
        {"stats": stats, "recs": recs, "jobs": jobs},
    )
