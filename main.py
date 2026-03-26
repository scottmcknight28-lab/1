#!/usr/bin/env python3
"""
Langton's Wine Auction Agent – entry point.

Usage
-----
    python main.py                        # interactive REPL
    python main.py --query "..."          # single query, print result and exit
    python main.py --config my.yaml       # use a custom config file
    python main.py --scrape               # scrape then exit
    python main.py --recommend            # print recommendations then exit
"""

import argparse
import logging
import sys
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Langton's Wine Auction Agent – monitor auctions and find bidding opportunities."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--query",
        metavar="PROMPT",
        help="Run a single query and print the result, then exit.",
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

    # Import here so logging is configured first
    from wine_agent.agent import WineAuctionAgent

    agent = WineAuctionAgent(config_path=config_path)

    if args.scrape:
        print("Scraping Langton's…")
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
        # Interactive REPL
        agent.run()


if __name__ == "__main__":
    main()
