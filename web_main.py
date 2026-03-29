#!/usr/bin/env python3
"""
web_main.py – Entrypoint for the Langton's Agent dashboard (FastAPI + uvicorn).

Usage:
    python web_main.py [--config CONFIG] [--host HOST] [--port PORT] [--reload]
"""
import argparse
import os
import sys
from pathlib import Path

import uvicorn
import yaml


DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"
DEFAULT_DB = Path(__file__).parent / "wine_auctions.db"
DEFAULT_REPORTS = Path(__file__).parent / "reports"


def _bootstrap_config(config_path: Path) -> None:
    """Create a default config.yaml if one doesn't exist yet."""
    if config_path.exists():
        return
    default = {
        "scraping": {
            "base_url": "https://www.langtons.com.au",
            "delay_seconds": 2,
            "max_pages_per_auction": 10,
        },
        "database": {
            "path": str(DEFAULT_DB),
        },
        "bidding": {
            "value_threshold_pct": 15,
            "max_bid_amount": 500,
            "preferred_producers": [],
            "preferred_regions": [],
            "condition_requirements": {
                "min_fill_level": "",
                "require_cellar_stored": False,
                "require_original_carton": False,
            },
        },
        "schedule": {
            "timezone": "Australia/Sydney",
            "tuesday_close": "20:00",
            "sunday_close": "20:00",
            "pre_auction_hours": 24,
            "post_auction_hours": 2,
            "reports_dir": str(DEFAULT_REPORTS),
            "email": {
                "enabled": False,
                "smtp_host": "",
                "smtp_port": 587,
                "username": "",
                "password": "",
                "from_addr": "",
                "to_addr": "",
            },
        },
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(default, f, default_flow_style=False, allow_unicode=True)
    print(f"[bootstrap] Created default config at {config_path}")


def _resolve_paths(config_path: Path) -> tuple[str, str, str]:
    """Return (config_path, db_path, reports_dir) as strings, reading db/reports from config."""
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    db_path = cfg.get("database", {}).get("path") or str(DEFAULT_DB)
    reports_dir = cfg.get("schedule", {}).get("reports_dir") or str(DEFAULT_REPORTS)
    Path(reports_dir).mkdir(parents=True, exist_ok=True)
    return str(config_path), db_path, reports_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Langton's Auction Agent – Web Dashboard")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.yaml")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload (dev only)")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    _bootstrap_config(config_path)
    config_str, db_path, reports_dir = _resolve_paths(config_path)

    # Pass paths to the app factory via environment variables so the factory
    # can access them without module-level globals (safe for --reload mode).
    os.environ["WINE_CONFIG_PATH"] = config_str
    os.environ["WINE_DB_PATH"] = db_path
    os.environ["WINE_REPORTS_DIR"] = reports_dir

    print(f"[web_main] config  : {config_str}")
    print(f"[web_main] database: {db_path}")
    print(f"[web_main] reports : {reports_dir}")
    print(f"[web_main] listening on http://{args.host}:{args.port}")

    uvicorn.run(
        "dashboard.app:get_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=1,          # SQLite + BackgroundScheduler are not fork-safe
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
