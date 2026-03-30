"""
Claude-powered wine auction agent.

The agent exposes six tools to Claude:
  - scrape_auctions            scrape Langton's and populate the DB
  - query_lots                 flexible lot search
  - get_market_average         historical price analytics
  - get_price_history          per-wine price timeline
  - evaluate_lot               run the bidding strategy on one lot
  - get_bidding_recommendations run strategy across all open lots
  - get_auction_stats          database overview
  - import_lots_csv            bulk-import lots from a CSV file

Claude uses these tools to answer questions, surface opportunities, and
explain its reasoning in natural language.
"""

import csv
import io
import json
import logging
from datetime import datetime
from typing import Optional

import anthropic
import yaml

from .database import AuctionDatabase
from .scraper import LangtonsScraper
from .strategy import BiddingStrategy

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are an expert wine auction analyst specialising in Langton's fine wine auctions.

You help the user to:
1. Monitor live and past Langton's auctions by scraping the website.
2. Maintain a local SQLite database of all auction results.
3. Analyse bidding opportunities using a configurable strategy that weighs:
   - Price vs historical market average (primary driver)
   - Provenance and condition (fill level, cellar storage, original carton)
   - Producer and region focus (user-defined whitelist)

When making recommendations always cite:
- The estimated price vs the historical market average
- The fill level and provenance details
- Your recommended maximum bid

Be concise but thorough.  When you don't have enough data (e.g. no historical
prices for a wine), say so clearly and explain what data would improve your
recommendations.
"""

# ------------------------------------------------------------------
# Tool definitions (raw JSON schema – used with manual agentic loop
# so we retain full control and can stream progress to the console)
# ------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "scrape_auctions",
        "description": (
            "Scrape Langton's website for auction listings and lot details. "
            "Saves results into the local database.  Returns a summary of what "
            "was found.  Use this to refresh data before making recommendations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_auctions": {
                    "type": "integer",
                    "description": "Maximum number of auctions to scrape (default 5).",
                },
                "fetch_lot_details": {
                    "type": "boolean",
                    "description": (
                        "If true, follow each lot link for richer condition data "
                        "(slower). Default false."
                    ),
                },
            },
        },
    },
    {
        "name": "query_lots",
        "description": (
            "Search the auction database. All filters are optional and combined with AND."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "producer":     {"type": "string", "description": "Partial producer name match."},
                "region":       {"type": "string", "description": "Partial region name match."},
                "wine_name":    {"type": "string", "description": "Partial wine name match."},
                "min_vintage":  {"type": "integer", "description": "Earliest vintage year."},
                "max_vintage":  {"type": "integer", "description": "Latest vintage year."},
                "auction_id":   {"type": "string",  "description": "Filter to a specific auction."},
                "current_only": {
                    "type": "boolean",
                    "description": "If true, only return lots without a realized price (still open).",
                },
            },
        },
    },
    {
        "name": "get_market_average",
        "description": (
            "Return the historical average realized price for a wine, "
            "optionally filtered to a specific vintage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "wine_name": {"type": "string", "description": "Wine name (partial match)."},
                "vintage":   {"type": "integer", "description": "Vintage year (optional)."},
            },
            "required": ["wine_name"],
        },
    },
    {
        "name": "get_price_history",
        "description": "Return a list of all realized prices for a wine across auctions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wine_name": {"type": "string"},
                "vintage":   {"type": "integer", "description": "Vintage year (optional)."},
            },
            "required": ["wine_name"],
        },
    },
    {
        "name": "evaluate_lot",
        "description": (
            "Run the configured bidding strategy against a single lot and return "
            "a score, bid flag, recommended maximum bid, and detailed reasoning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lot_id": {
                    "type": "integer",
                    "description": "Database ID of the lot (from query_lots results).",
                }
            },
            "required": ["lot_id"],
        },
    },
    {
        "name": "get_bidding_recommendations",
        "description": (
            "Run the bidding strategy across all currently open lots (no realized price) "
            "and return ranked recommendations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "min_score": {
                    "type": "number",
                    "description": "Minimum strategy score to include (0–1, default 0.40).",
                }
            },
        },
    },
    {
        "name": "get_auction_stats",
        "description": "Return database statistics: total auctions, lots, top producers, etc.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "import_lots_csv",
        "description": (
            "Bulk-import auction lot data from a CSV string.  Useful for pasting "
            "exported Langton's data.  Expected columns (case-insensitive): "
            "auction_id, lot_number, wine_name, producer, vintage, region, "
            "estimate_low, estimate_high, realized_price, condition_notes, "
            "fill_level, cellar_stored, original_carton."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "csv_data": {
                    "type": "string",
                    "description": "Raw CSV text (first row = headers).",
                },
                "auction_id": {
                    "type": "string",
                    "description": "Auction ID to associate all lots with.",
                },
                "auction_title": {
                    "type": "string",
                    "description": "Human-readable auction title (optional).",
                },
            },
            "required": ["csv_data", "auction_id"],
        },
    },
]


class WineAuctionAgent:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        self.client   = anthropic.Anthropic()
        self.db       = AuctionDatabase(cfg.get("database", {}).get("path", "wine_auction.db"))
        self.scraper  = LangtonsScraper(
            base_url=cfg.get("scraping", {}).get("base_url", "https://www.langtons.com.au"),
            delay_seconds=cfg.get("scraping", {}).get("delay_seconds", 2.0),
            max_pages_per_auction=cfg.get("scraping", {}).get("max_pages_per_auction", 10),
        )
        self.strategy = BiddingStrategy(config_path)
        self.history: list[dict] = []

    # ------------------------------------------------------------------
    # Public: interactive REPL
    # ------------------------------------------------------------------

    def run(self) -> None:
        from rich.console import Console
        from rich.markdown import Markdown
        from rich.panel import Panel

        console = Console()
        console.print(
            Panel(
                "[bold green]Langton's Wine Auction Agent[/bold green]\n"
                "Powered by Claude Opus 4.6 with adaptive thinking.\n"
                "Type [bold]help[/bold] for example commands, [bold]quit[/bold] to exit.",
                border_style="green",
            )
        )

        while True:
            try:
                user_input = console.input("\n[bold blue]You:[/bold blue] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[yellow]Goodbye![/yellow]")
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                console.print("[green]Goodbye![/green]")
                break
            if user_input.lower() == "help":
                self._print_help(console)
                continue

            self.history.append({"role": "user", "content": user_input})
            console.print()

            reply = self._agent_loop(console)
            self.history.append({"role": "assistant", "content": reply})

        self.db.close()

    # ------------------------------------------------------------------
    # Public: single query (for scripting / testing)
    # ------------------------------------------------------------------

    def query(self, prompt: str) -> str:
        self.history.append({"role": "user", "content": prompt})
        return self._agent_loop(console=None)

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    def _agent_loop(self, console) -> str:
        messages = list(self.history)
        final_text = ""

        while True:
            if console:
                from rich.console import Console as RichConsole
                status_ctx = console.status("[dim]Thinking…[/dim]", spinner="dots")
            else:
                status_ctx = _NullContext()

            with status_ctx:
                response = self.client.messages.create(
                    model=MODEL,
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )

            tool_results: list[dict] = []

            for block in response.content:
                if block.type == "thinking":
                    # Silently discard thinking blocks from display
                    pass
                elif block.type == "text":
                    final_text = block.text
                    if console:
                        from rich.markdown import Markdown
                        console.print(f"[bold yellow]Agent:[/bold yellow]")
                        console.print(Markdown(block.text))
                elif block.type == "tool_use":
                    if console:
                        console.print(
                            f"[dim]  ↳ calling [bold]{block.name}[/bold] …[/dim]"
                        )
                    result = self._dispatch(block.name, block.input)
                    result_text = (
                        json.dumps(result, default=str)
                        if not isinstance(result, str)
                        else result
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        }
                    )

            if response.stop_reason == "end_turn" or not tool_results:
                return final_text

            # Feed tool results back and continue
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, name: str, inp: dict) -> dict:
        try:
            handler = {
                "scrape_auctions":          self._t_scrape_auctions,
                "query_lots":               self._t_query_lots,
                "get_market_average":       self._t_market_average,
                "get_price_history":        self._t_price_history,
                "evaluate_lot":             self._t_evaluate_lot,
                "get_bidding_recommendations": self._t_recommendations,
                "get_auction_stats":        self._t_stats,
                "import_lots_csv":          self._t_import_csv,
            }.get(name)
            if handler is None:
                return {"error": f"Unknown tool: {name}"}
            return handler(**inp)
        except Exception as exc:
            logger.exception("Tool %s raised: %s", name, exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _t_scrape_auctions(
        self,
        max_auctions: int = 5,
        fetch_lot_details: bool = False,
    ) -> dict:
        auctions = self.scraper.get_auctions()
        summary = []

        for auction in auctions[:max_auctions]:
            self.db.upsert_auction(auction)
            lots = self.scraper.get_lots(auction["url"], auction["auction_id"])

            if fetch_lot_details:
                detailed: list[dict] = []
                for lot in lots:
                    if lot.get("lot_url"):
                        detail = self.scraper.get_lot_detail(
                            lot["lot_url"], auction["auction_id"]
                        )
                        if detail:
                            detailed.append(detail)
                        else:
                            detailed.append(lot)
                    else:
                        detailed.append(lot)
                lots = detailed

            for lot in lots:
                self.db.upsert_lot(lot)

            summary.append(
                {
                    "auction_id": auction["auction_id"],
                    "title":      auction["title"],
                    "lots_saved": len(lots),
                }
            )

        return {
            "auctions_processed": len(summary),
            "auctions": summary,
            "total_lots": sum(a["lots_saved"] for a in summary),
        }

    def _t_query_lots(
        self,
        producer: Optional[str] = None,
        region: Optional[str] = None,
        wine_name: Optional[str] = None,
        min_vintage: Optional[int] = None,
        max_vintage: Optional[int] = None,
        auction_id: Optional[str] = None,
        current_only: bool = False,
    ) -> dict:
        lots = self.db.search_lots(
            producer=producer,
            region=region,
            wine_name=wine_name,
            min_vintage=min_vintage,
            max_vintage=max_vintage,
            auction_id=auction_id,
            current_only=current_only,
        )
        return {"count": len(lots), "lots": lots}

    def _t_market_average(self, wine_name: str, vintage: Optional[int] = None) -> dict:
        avg = self.db.get_market_average(wine_name, vintage)
        return {
            "wine_name":      wine_name,
            "vintage":        vintage,
            "market_average": round(avg, 2) if avg else None,
            "currency":       "AUD",
        }

    def _t_price_history(self, wine_name: str, vintage: Optional[int] = None) -> dict:
        rows = self.db.get_price_history(wine_name, vintage)
        return {"wine_name": wine_name, "vintage": vintage, "history": rows}

    def _t_evaluate_lot(self, lot_id: int) -> dict:
        lot = self.db.get_lot_by_id(lot_id)
        if not lot:
            return {"error": f"Lot {lot_id} not found in database."}
        avg = self.db.get_market_average(lot["wine_name"], lot.get("vintage"))
        evaluation = self.strategy.evaluate(lot, avg)
        if evaluation["bid"] and evaluation["max_bid"]:
            self.db.save_recommendation(
                lot_id,
                evaluation["max_bid"],
                evaluation["score"],
                evaluation["reasons"],
            )
        return {"lot": lot, "market_average_aud": avg, "evaluation": evaluation}

    def _t_recommendations(self, min_score: float = 0.40) -> dict:
        open_lots = self.db.search_lots(current_only=True)
        recs = self.strategy.bulk_evaluate(
            open_lots,
            get_market_avg=lambda name, v: self.db.get_market_average(name, v),
            min_score=min_score,
        )
        # Persist recommendations
        for r in recs:
            if r.get("lot_id") and r.get("max_bid"):
                self.db.save_recommendation(
                    r["lot_id"], r["max_bid"], r["score"], r["reasons"]
                )
        return {"count": len(recs), "recommendations": recs}

    def _t_stats(self) -> dict:
        return self.db.get_stats()

    def _t_import_csv(
        self,
        csv_data: str,
        auction_id: str,
        auction_title: Optional[str] = None,
    ) -> dict:
        # Ensure auction exists
        self.db.upsert_auction(
            {
                "auction_id":   auction_id,
                "title":        auction_title or f"Imported – {auction_id}",
                "auction_date": datetime.now().strftime("%Y-%m-%d"),
                "url":          "",
                "scraped_at":   datetime.now().isoformat(),
            }
        )

        reader = csv.DictReader(io.StringIO(csv_data))
        # Normalise header names to lower-case stripped
        rows = [{k.strip().lower(): v.strip() for k, v in row.items()} for row in reader]

        imported = 0
        errors: list[str] = []
        for i, row in enumerate(rows, 1):
            try:
                def _float(key: str) -> Optional[float]:
                    val = row.get(key, "").replace("$", "").replace(",", "")
                    return float(val) if val else None

                def _int(key: str) -> Optional[int]:
                    val = row.get(key, "").strip()
                    return int(val) if val and val.isdigit() else None

                lot = {
                    "auction_id":      auction_id,
                    "lot_number":      row.get("lot_number", ""),
                    "wine_name":       row.get("wine_name") or row.get("wine", ""),
                    "producer":        row.get("producer", ""),
                    "vintage":         _int("vintage"),
                    "region":          row.get("region", ""),
                    "varietal":        row.get("varietal", ""),
                    "bottle_count":    _int("bottle_count") or 1,
                    "bottle_size":     row.get("bottle_size", "750ml"),
                    "estimate_low":    _float("estimate_low"),
                    "estimate_high":   _float("estimate_high"),
                    "realized_price":  _float("realized_price"),
                    "condition_notes": row.get("condition_notes", ""),
                    "fill_level":      row.get("fill_level", ""),
                    "cellar_stored":   1 if row.get("cellar_stored", "").lower()
                                           in ("1", "yes", "true") else 0,
                    "original_carton": 1 if row.get("original_carton", "").lower()
                                           in ("1", "yes", "true") else 0,
                    "provenance":      row.get("provenance", ""),
                    "lot_url":         row.get("lot_url", ""),
                }
                if not lot["wine_name"]:
                    errors.append(f"Row {i}: missing wine_name – skipped")
                    continue
                self.db.upsert_lot(lot)
                imported += 1
            except Exception as exc:
                errors.append(f"Row {i}: {exc}")

        return {
            "auction_id": auction_id,
            "imported":   imported,
            "skipped":    len(errors),
            "errors":     errors[:10],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _print_help(console) -> None:
        console.print(
            "\n[bold]Example commands:[/bold]\n"
            "  Scrape Langton's for current auctions\n"
            "  Show me all Penfolds lots\n"
            "  What is the market average for Penfolds Grange 2010?\n"
            "  Find bidding opportunities in the current auction\n"
            "  Evaluate lot 42\n"
            "  Import CSV data for auction ABC123\n"
            "  Show database statistics\n"
        )


class _NullContext:
    """No-op context manager (used when running without a Rich console)."""
    def __enter__(self): return self
    def __exit__(self, *_): pass
