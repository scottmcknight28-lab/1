from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

router = APIRouter()


def _reports_path(request: Request) -> Path:
    p = Path(request.app.state.reports_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


@router.get("/reports", include_in_schema=False)
async def reports_page(request: Request):
    rdir  = _reports_path(request)
    files = sorted(rdir.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    items = [
        {
            "name":    f.name,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "mtime":   __import__("datetime").datetime.fromtimestamp(
                           f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
        for f in files
    ]
    return request.app.state.templates.TemplateResponse(
        request, "reports.html", {"reports": items}
    )


@router.get("/reports/view/{filename}", include_in_schema=False)
async def report_view(request: Request, filename: str):
    safe = Path(filename).name          # strip any path traversal
    fpath = _reports_path(request) / safe
    if not fpath.exists():
        raise HTTPException(404, "Report not found")
    content = fpath.read_text(errors="replace")
    return request.app.state.templates.TemplateResponse(
        request, "report_view.html", {"filename": safe, "content": content}
    )


@router.get("/reports/download/{filename}")
async def report_download(request: Request, filename: str):
    safe  = Path(filename).name
    fpath = _reports_path(request) / safe
    if not fpath.exists():
        raise HTTPException(404, "Report not found")
    return FileResponse(str(fpath), media_type="text/plain", filename=safe)


@router.post("/api/report/pre")
async def trigger_pre(request: Request):
    from wine_agent.reporter import AuctionReporter
    AuctionReporter(request.app.state.config_path).run_pre_auction_report()
    return {"status": "ok", "type": "pre"}


@router.post("/api/report/post")
async def trigger_post(request: Request):
    from wine_agent.reporter import AuctionReporter
    AuctionReporter(request.app.state.config_path).run_post_auction_report()
    return {"status": "ok", "type": "post"}
