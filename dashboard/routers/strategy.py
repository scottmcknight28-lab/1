from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from typing import Optional
from dashboard.config_manager import read_config, write_config

router = APIRouter()


@router.get("/strategy", include_in_schema=False)
async def strategy_page(request: Request, saved: bool = False):
    cfg = read_config(request.app.state.config_path)
    b   = cfg.get("bidding", {})
    cond = b.get("condition_requirements", {})
    return request.app.state.templates.TemplateResponse(
        "strategy.html",
        {
            "request": request,
            "saved": saved,
            "value_threshold_pct": b.get("value_threshold_pct", 15),
            "max_bid_amount":      b.get("max_bid_amount", 500),
            "preferred_producers": "\n".join(b.get("preferred_producers", [])),
            "preferred_regions":   "\n".join(b.get("preferred_regions", [])),
            "min_fill_level":      cond.get("min_fill_level", ""),
            "require_cellar_stored":   bool(cond.get("require_cellar_stored", False)),
            "require_original_carton": bool(cond.get("require_original_carton", False)),
        },
    )


@router.post("/strategy")
async def strategy_save(
    request:                 Request,
    value_threshold_pct:     float = Form(...),
    max_bid_amount:          float = Form(...),
    preferred_producers:     str   = Form(""),
    preferred_regions:       str   = Form(""),
    min_fill_level:          str   = Form(""),
    require_cellar_stored:   Optional[str] = Form(None),
    require_original_carton: Optional[str] = Form(None),
):
    def _lines(text: str) -> list[str]:
        return [l.strip() for l in text.splitlines() if l.strip()]

    fill_options = [
        "into neck", "base of neck", "top shoulder",
        "upper shoulder", "mid shoulder", "lower shoulder", ""
    ]
    if min_fill_level not in fill_options:
        min_fill_level = ""

    cfg = read_config(request.app.state.config_path)
    cfg.setdefault("bidding", {}).update({
        "value_threshold_pct": max(0.0, min(100.0, value_threshold_pct)),
        "max_bid_amount":      max(0.0, max_bid_amount),
        "preferred_producers": _lines(preferred_producers),
        "preferred_regions":   _lines(preferred_regions),
        "condition_requirements": {
            "min_fill_level":          min_fill_level,
            "require_cellar_stored":   require_cellar_stored == "on",
            "require_original_carton": require_original_carton == "on",
        },
    })
    write_config(request.app.state.config_path, cfg)
    return RedirectResponse("/strategy?saved=1", status_code=303)


@router.get("/api/strategy")
async def api_strategy_get(request: Request):
    return read_config(request.app.state.config_path).get("bidding", {})
