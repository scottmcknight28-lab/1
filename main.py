#!/usr/bin/env python3
"""
Langton's Wine Auction Agent – entry point.

Usage
-----
    python main.py                        # interactive REPL
    python main.py --daemon               # run scheduled reports (blocks until Ctrl-C)
    python main.py --query "..."          # single query, print result and exit
    python main.py --config my.yaml       # use a custom config file
    python main.py --scrape               # scrape then exit
    python main.py --recommend            # print recommendations then exit
    python main.py --report pre           # generate pre-auction report now and exit
    python main.py --report post          # generate post-auction report now and exit
"""

import argparse
import logging
import sys
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quieten noisy third-party loggers unless verbose
    if not verbose:
        for name in ("apscheduler", "urllib3", "requests"):
            logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Langton's Wine Auction Agent – monitor auctions and find bidding opportunities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help=(
            "Run as a background scheduler. Automatically scrapes results after "
            "each auction and emails a 24-hour pre-auction report. Blocks until "
            "Ctrl-C or SIGTERM."
        ),
    )
    parser.add_argument(
        "--report",
        choices=["pre", "post"],
        metavar="TYPE",
        help=(
            "Generate a report immediately and exit. "
            "TYPE=pre: pre-auction bidding report. "
            "TYPE=post: post-auction results harvest."
        ),
    )
    parser.add_argument(
        "--query",
        metavar="PROMPT",
        help="Run a single natural-language query and print the result, then exit.",
    )
    parser.add_argument(
        "--scrape",
        action="store_true",
        help="Scrape Langton's for current auctions, update the database, then exit.",
    )
    parser.add_argument(
        "--recommend",
        action="store_true",
        help="Print bidding recommendations for all currently open lots, then exit.",
    )
    parser.add_argument(
        "--schedule-info",
        action="store_true",
        help="Print the upcoming scheduled job times and exit.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    _setup_logging(args.verbose)

    config_path = args.config
    if not Path(config_path).exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        print("Copy config.yaml to this directory and edit it before running.", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Scheduler / daemon mode
    # ------------------------------------------------------------------
    if args.daemon:
        from wine_agent.scheduler import run_daemon
        run_daemon(config_path)
        return

    if args.schedule_info:
        from wine_agent.scheduler import build_scheduler
        sched = build_scheduler(config_path)
        from wine_agent.scheduler import _print_schedule
        _print_schedule(sched)
        return

    # ------------------------------------------------------------------
    # On-demand report generation
    # ------------------------------------------------------------------
    if args.report:
        from wine_agent.reporter import AuctionReporter
        reporter = AuctionReporter(config_path)
        if args.report == "pre":
            reporter.run_pre_auction_report()
        else:
            reporter.run_post_auction_report()
        return

    # ------------------------------------------------------------------
    # Agent modes
    # ------------------------------------------------------------------
    from wine_agent.agent import WineAuctionAgent
    agent = WineAuctionAgent(config_path=config_path)

    if args.scrape:
        result = agent.query("Scrape Langton's for current auctions and show me a summary.")
        print(result)

    elif args.recommend:
        result = agent.query(
            "Find all bidding opportunities in the currently open lots and give me a "
            "ranked list with recommended maximum bids and your reasoning."
        )
        print(result)

    elif args.query:
        result = agent.query(args.query)
        print(result)

    else:
        agent.run()


if __name__ == "__main__":
    main()
