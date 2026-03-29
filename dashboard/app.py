"""FastAPI application factory."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from .scheduler_bridge import build_background_scheduler
from .routers import dashboard, lots, upcoming, strategy, reports


def create_app(
    config_path: str = "config.yaml",
    db_path: str    = "wine_auction.db",
    reports_dir: str = "reports",
) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start background scheduler
        sched = build_background_scheduler(config_path)
        sched.start()
        app.state.scheduler = sched
        yield
        sched.shutdown(wait=False)

    app = FastAPI(
        title="Langton's Wine Auction Agent",
        lifespan=lifespan,
    )

    # Make paths available to all routers via app.state
    app.state.config_path  = config_path
    app.state.db_path      = db_path
    app.state.reports_dir  = reports_dir

    # Templates
    tmpl_dir = Path(__file__).parent / "templates"
    app.state.templates = Jinja2Templates(directory=str(tmpl_dir))

    # Routers
    app.include_router(dashboard.router)
    app.include_router(lots.router)
    app.include_router(upcoming.router)
    app.include_router(strategy.router)
    app.include_router(reports.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


def get_app() -> FastAPI:
    """Factory called by uvicorn when factory=True.  Reads paths from env vars set by web_main.py."""
    return create_app(
        config_path=os.environ.get("WINE_CONFIG_PATH", "config.yaml"),
        db_path=os.environ.get("WINE_DB_PATH", "wine_auctions.db"),
        reports_dir=os.environ.get("WINE_REPORTS_DIR", "reports"),
    )
