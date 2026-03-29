"""
BackgroundScheduler bridge – runs auction jobs alongside FastAPI/uvicorn.

Uses APScheduler's BackgroundScheduler (non-blocking, runs in daemon threads)
instead of the BlockingScheduler used by the CLI daemon.  Jobs create a fresh
AuctionReporter on each invocation so they always pick up the latest config.
"""

import logging
from datetime import datetime, timedelta

import pytz
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def _offset_dow_time(base_dow: int, base_hour: int, base_minute: int, offset_hours: int):
    anchor = datetime(2000, 1, 3 + base_dow, base_hour, base_minute)
    result = anchor + timedelta(hours=offset_hours)
    return result.weekday(), result.hour, result.minute


def build_background_scheduler(config_path: str) -> BackgroundScheduler:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    s = cfg.get("schedule", {})
    tz        = pytz.timezone(s.get("timezone", "Australia/Sydney"))
    pre_h     = int(s.get("pre_auction_hours", 24))
    post_h    = int(s.get("post_auction_hours", 2))
    tue_h, tue_m = _parse_hhmm(s.get("tuesday_close", "20:00"))
    sun_h, sun_m = _parse_hhmm(s.get("sunday_close",  "20:00"))

    scheduler = BackgroundScheduler(timezone=tz)

    # --- helpers ---
    def pre_job():
        try:
            from wine_agent.reporter import AuctionReporter
            AuctionReporter(config_path).run_pre_auction_report()
        except Exception as exc:
            logger.exception("Pre-auction report failed: %s", exc)

    def post_job():
        try:
            from wine_agent.reporter import AuctionReporter
            AuctionReporter(config_path).run_post_auction_report()
        except Exception as exc:
            logger.exception("Post-auction report failed: %s", exc)

    def daily_job():
        try:
            from wine_agent.reporter import AuctionReporter
            r = AuctionReporter(config_path)
            for auction in r.scraper.get_auctions()[:3]:
                r.db.upsert_auction(auction)
                for lot in r.scraper.get_lots(auction["url"], auction["auction_id"]):
                    r.db.upsert_lot(lot)
            logger.info("Daily keep-alive scrape done.")
        except Exception as exc:
            logger.exception("Daily scrape failed: %s", exc)

    # Pre-auction reports (24 h before each close)
    pre_tue = _offset_dow_time(1, tue_h, tue_m, -pre_h)
    pre_sun = _offset_dow_time(6, sun_h, sun_m, -pre_h)
    scheduler.add_job(pre_job, CronTrigger(day_of_week=pre_tue[0], hour=pre_tue[1],
                                           minute=pre_tue[2], timezone=tz),
                      id="pre_tue", name="Pre-auction (Tuesday)", misfire_grace_time=3600,
                      replace_existing=True)
    scheduler.add_job(pre_job, CronTrigger(day_of_week=pre_sun[0], hour=pre_sun[1],
                                           minute=pre_sun[2], timezone=tz),
                      id="pre_sun", name="Pre-auction (Sunday)", misfire_grace_time=3600,
                      replace_existing=True)

    # Post-auction harvests (2 h after each close)
    post_tue = _offset_dow_time(1, tue_h, tue_m, post_h)
    post_sun = _offset_dow_time(6, sun_h, sun_m, post_h)
    scheduler.add_job(post_job, CronTrigger(day_of_week=post_tue[0], hour=post_tue[1],
                                            minute=post_tue[2], timezone=tz),
                      id="post_tue", name="Post-auction harvest (Tuesday)", misfire_grace_time=3600,
                      replace_existing=True)
    scheduler.add_job(post_job, CronTrigger(day_of_week=post_sun[0], hour=post_sun[1],
                                            minute=post_sun[2], timezone=tz),
                      id="post_sun", name="Post-auction harvest (Sunday)", misfire_grace_time=3600,
                      replace_existing=True)

    # Daily keep-alive at 08:00
    scheduler.add_job(daily_job, CronTrigger(hour=8, minute=0, timezone=tz),
                      id="daily", name="Daily scrape (08:00)", misfire_grace_time=3600,
                      replace_existing=True)

    return scheduler


def get_job_schedule(scheduler: BackgroundScheduler) -> list[dict]:
    jobs = []
    for job in scheduler.get_jobs():
        nxt = job.next_run_time
        jobs.append({
            "id":       job.id,
            "name":     job.name,
            "next_run": nxt.strftime("%a %-d %b %Y  %-I:%M %p %Z") if nxt else "—",
            "next_ts":  nxt.timestamp() if nxt else 0,
        })
    return sorted(jobs, key=lambda j: j["next_ts"])
