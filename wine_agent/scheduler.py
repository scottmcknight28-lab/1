"""
APScheduler-based daemon for the wine auction agent.

Schedule (all times in the configured timezone, default Australia/Sydney):

  Auctions close on Tuesday and Sunday evenings.
  The close time is read from config.yaml (schedule.tuesday_close / sunday_close).

  PRE-AUCTION report  → 24 h before close  (configurable: schedule.pre_auction_hours)
    Tuesday close 20:00  →  Monday    20:00
    Sunday  close 20:00  →  Saturday  20:00

  POST-AUCTION scrape → 2 h after close    (configurable: schedule.post_auction_hours)
    Tuesday close 20:00  →  Tuesday   22:00
    Sunday  close 20:00  →  Sunday    22:00

A daily "keep-alive" scrape also runs each morning at 08:00 to catch any
mid-week lots or schedule changes.
"""

import logging
import signal
import sys
from datetime import datetime, timedelta, time as dt_time

import pytz
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from rich.console import Console

from .reporter import AuctionReporter

logger = logging.getLogger(__name__)
console = Console()

# Day-of-week abbreviations used by APScheduler cron
_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_hhmm(s: str) -> tuple[int, int]:
    """'20:00' → (20, 0)"""
    h, m = s.strip().split(":")
    return int(h), int(m)


def _offset_dow_time(
    base_dow: int, base_hour: int, base_minute: int, offset_hours: int
) -> tuple[int, int, int]:
    """
    Compute (day_of_week, hour, minute) after applying +/- offset_hours
    to a (day_of_week, hour, minute) anchor.
    """
    anchor = datetime(2000, 1, 3 + base_dow, base_hour, base_minute)  # Mon=3 Jan 2000
    result = anchor + timedelta(hours=offset_hours)
    return result.weekday(), result.hour, result.minute


def build_scheduler(config_path: str = "config.yaml") -> BlockingScheduler:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    sched_cfg = cfg.get("schedule", {})
    tz_name   = sched_cfg.get("timezone", "Australia/Sydney")
    tz        = pytz.timezone(tz_name)

    pre_hours  = int(sched_cfg.get("pre_auction_hours",  24))
    post_hours = int(sched_cfg.get("post_auction_hours",  2))

    tue_h, tue_m = _parse_hhmm(sched_cfg.get("tuesday_close", "20:00"))
    sun_h, sun_m = _parse_hhmm(sched_cfg.get("sunday_close",  "20:00"))

    reporter = AuctionReporter(config_path)
    scheduler = BlockingScheduler(timezone=tz)

    # ------------------------------------------------------------------
    # Pre-auction reports  (24 h before Tuesday and Sunday close)
    # ------------------------------------------------------------------
    pre_tue_dow, pre_tue_h, pre_tue_m = _offset_dow_time(1, tue_h, tue_m, -pre_hours)
    pre_sun_dow, pre_sun_h, pre_sun_m = _offset_dow_time(6, sun_h, sun_m, -pre_hours)

    scheduler.add_job(
        _run_pre_report,
        trigger=CronTrigger(
            day_of_week=pre_tue_dow, hour=pre_tue_h, minute=pre_tue_m, timezone=tz
        ),
        args=[reporter],
        id="pre_auction_tuesday",
        name=f"Pre-auction report (Tuesday close – {pre_hours}h before)",
        misfire_grace_time=3600,
        replace_existing=True,
    )
    scheduler.add_job(
        _run_pre_report,
        trigger=CronTrigger(
            day_of_week=pre_sun_dow, hour=pre_sun_h, minute=pre_sun_m, timezone=tz
        ),
        args=[reporter],
        id="pre_auction_sunday",
        name=f"Pre-auction report (Sunday close – {pre_hours}h before)",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # ------------------------------------------------------------------
    # Post-auction results harvests  (2 h after close)
    # ------------------------------------------------------------------
    post_tue_dow, post_tue_h, post_tue_m = _offset_dow_time(1, tue_h, tue_m, post_hours)
    post_sun_dow, post_sun_h, post_sun_m = _offset_dow_time(6, sun_h, sun_m, post_hours)

    scheduler.add_job(
        _run_post_report,
        trigger=CronTrigger(
            day_of_week=post_tue_dow, hour=post_tue_h, minute=post_tue_m, timezone=tz
        ),
        args=[reporter],
        id="post_auction_tuesday",
        name=f"Post-auction harvest (Tuesday close + {post_hours}h)",
        misfire_grace_time=3600,
        replace_existing=True,
    )
    scheduler.add_job(
        _run_post_report,
        trigger=CronTrigger(
            day_of_week=post_sun_dow, hour=post_sun_h, minute=post_sun_m, timezone=tz
        ),
        args=[reporter],
        id="post_auction_sunday",
        name=f"Post-auction harvest (Sunday close + {post_hours}h)",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # ------------------------------------------------------------------
    # Daily keep-alive scrape  (08:00 every morning)
    # ------------------------------------------------------------------
    scheduler.add_job(
        _run_daily_scrape,
        trigger=CronTrigger(hour=8, minute=0, timezone=tz),
        args=[reporter],
        id="daily_scrape",
        name="Daily keep-alive scrape (08:00)",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    return scheduler


def run_daemon(config_path: str = "config.yaml") -> None:
    """Start the scheduler and block until SIGINT/SIGTERM."""
    scheduler = build_scheduler(config_path)

    def _shutdown(signum, frame):
        console.print("\n[yellow]Shutdown signal received – stopping scheduler…[/yellow]")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _print_schedule(scheduler)
    console.print("[green]Scheduler running.  Press Ctrl-C to stop.[/green]\n")
    scheduler.start()


# ------------------------------------------------------------------
# Job functions
# ------------------------------------------------------------------

def _run_pre_report(reporter: AuctionReporter) -> None:
    console.rule("[bold green]Pre-Auction Report[/bold green]")
    try:
        reporter.run_pre_auction_report()
    except Exception as exc:
        logger.exception("Pre-auction report failed: %s", exc)
        console.print(f"[red]Pre-auction report error: {exc}[/red]")


def _run_post_report(reporter: AuctionReporter) -> None:
    console.rule("[bold blue]Post-Auction Results Harvest[/bold blue]")
    try:
        reporter.run_post_auction_report()
    except Exception as exc:
        logger.exception("Post-auction report failed: %s", exc)
        console.print(f"[red]Post-auction report error: {exc}[/red]")


def _run_daily_scrape(reporter: AuctionReporter) -> None:
    console.print(f"[dim]{datetime.now():%H:%M}  Daily scrape starting…[/dim]")
    try:
        auctions = reporter.scraper.get_auctions()
        for auction in auctions[:3]:
            reporter.db.upsert_auction(auction)
            lots = reporter.scraper.get_lots(auction["url"], auction["auction_id"])
            for lot in lots:
                reporter.db.upsert_lot(lot)
        console.print(f"[dim]Daily scrape done.[/dim]")
    except Exception as exc:
        logger.exception("Daily scrape failed: %s", exc)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _print_schedule(scheduler: BlockingScheduler) -> None:
    console.print("\n[bold]Scheduled jobs:[/bold]")
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        next_str = next_run.strftime("%a %-d %b %Y  %-I:%M %p %Z") if next_run else "not scheduled"
        console.print(f"  [cyan]{job.name}[/cyan]")
        console.print(f"    next run: [green]{next_str}[/green]")
    console.print()
