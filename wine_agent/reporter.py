"""
Report generation for the wine auction agent.

Two report types:
  PRE-AUCTION  – produced 24 h before close; ranked bidding recommendations
  POST-AUCTION – produced 2 h after close; results harvest + strategy accuracy

Reports are:
  • printed to the terminal (Rich)
  • saved as .txt files in reports/
  • optionally emailed (configure smtp_* in config.yaml)
"""

import logging
import smtplib
import textwrap
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from .database import AuctionDatabase
from .scraper import LangtonsScraper
from .strategy import BiddingStrategy

logger = logging.getLogger(__name__)


class AuctionReporter:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            self.cfg = yaml.safe_load(f)

        sched = self.cfg.get("schedule", {})
        self.reports_dir = Path(sched.get("reports_dir", "reports"))
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        db_path = self.cfg.get("database", {}).get("path", "wine_auction.db")
        self.db       = AuctionDatabase(db_path)
        self.scraper  = LangtonsScraper(
            base_url=self.cfg.get("scraping", {}).get("base_url", "https://www.langtons.com.au"),
            delay_seconds=self.cfg.get("scraping", {}).get("delay_seconds", 2.0),
            max_pages_per_auction=self.cfg.get("scraping", {}).get("max_pages_per_auction", 10),
        )
        self.strategy = BiddingStrategy(config_path)

    # ------------------------------------------------------------------
    # Public entry points (called by the scheduler)
    # ------------------------------------------------------------------

    def run_pre_auction_report(self) -> str:
        """Scrape the upcoming auction and produce a bidding recommendations report."""
        logger.info("Starting pre-auction report")
        console = Console(record=True)

        # 1. Refresh upcoming lots
        console.print("[dim]Scraping Langton's for upcoming auction lots…[/dim]")
        auctions = self.scraper.get_auctions()
        total_new = 0
        for auction in auctions[:3]:
            self.db.upsert_auction(auction)
            lots = self.scraper.get_lots(auction["url"], auction["auction_id"])
            for lot in lots:
                self.db.upsert_lot(lot)
            total_new += len(lots)
        console.print(f"[dim]Refreshed {total_new} lots across {min(len(auctions), 3)} auctions.[/dim]\n")

        # 2. Evaluate open lots
        open_lots = self.db.search_lots(current_only=True)
        recommendations = self.strategy.bulk_evaluate(
            open_lots,
            get_market_avg=lambda n, v: self.db.get_market_average(n, v),
            min_score=0.40,
        )

        # 3. Render report
        now = datetime.now()
        title = f"PRE-AUCTION BIDDING REPORT  –  {now.strftime('%A %-d %B %Y, %-I:%M %p')}"
        self._render_pre_auction(console, title, open_lots, recommendations)

        report_text = console.export_text()
        filename = self.reports_dir / f"pre_auction_{now.strftime('%Y%m%d_%H%M')}.txt"
        filename.write_text(report_text)
        logger.info("Pre-auction report saved: %s", filename)

        self._maybe_email(
            subject=f"Langton's Pre-Auction Report – {now.strftime('%d %b %Y')}",
            body=report_text,
        )
        return report_text

    def run_post_auction_report(self) -> str:
        """Scrape auction results, update the DB, and produce a results analysis report."""
        logger.info("Starting post-auction report")
        console = Console(record=True)

        # 1. Snapshot of what we had recommended BEFORE results came in
        prior_recs = {r["lot_id"]: r for r in self.db.get_recommendations()}

        # 2. Scrape results (lots now have realized prices)
        console.print("[dim]Scraping Langton's for auction results…[/dim]")
        auctions = self.scraper.get_auctions()
        lots_updated = 0
        for auction in auctions[:5]:
            self.db.upsert_auction(auction)
            lots = self.scraper.get_lots(auction["url"], auction["auction_id"])
            for lot in lots:
                self.db.upsert_lot(lot)
            lots_updated += len([l for l in lots if l.get("realized_price")])
        console.print(f"[dim]Updated {lots_updated} lots with realized prices.[/dim]\n")

        # 3. Render report
        now = datetime.now()
        title = f"POST-AUCTION RESULTS REPORT  –  {now.strftime('%A %-d %B %Y, %-I:%M %p')}"
        self._render_post_auction(console, title, prior_recs)

        report_text = console.export_text()
        filename = self.reports_dir / f"post_auction_{now.strftime('%Y%m%d_%H%M')}.txt"
        filename.write_text(report_text)
        logger.info("Post-auction report saved: %s", filename)

        self._maybe_email(
            subject=f"Langton's Post-Auction Results – {now.strftime('%d %b %Y')}",
            body=report_text,
        )
        return report_text

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _render_pre_auction(
        self,
        console: Console,
        title: str,
        open_lots: list[dict],
        recommendations: list[dict],
    ) -> None:
        console.print(Panel(f"[bold green]{title}[/bold green]", border_style="green"))
        console.print(
            f"[bold]Open lots:[/bold] {len(open_lots)}   "
            f"[bold]Recommended:[/bold] {len(recommendations)}\n"
        )

        if not recommendations:
            console.print(
                "[yellow]No lots meet the current bidding criteria.[/yellow]\n"
                "Consider lowering [bold]value_threshold_pct[/bold] or broadening "
                "producer/region lists in [bold]config.yaml[/bold]."
            )
            return

        # Summary table
        tbl = Table(
            box=box.SIMPLE_HEAD,
            show_footer=False,
            title="[bold]Recommended Lots[/bold]",
            title_style="bold white",
        )
        tbl.add_column("#",          style="dim",    width=3,  no_wrap=True)
        tbl.add_column("Lot",        style="cyan",   width=5,  no_wrap=True)
        tbl.add_column("Wine",       style="white",  min_width=28)
        tbl.add_column("Vintage",    style="yellow", width=7,  no_wrap=True)
        tbl.add_column("Estimate",   style="green",  width=12, no_wrap=True)
        tbl.add_column("Mkt Avg",    style="blue",   width=10, no_wrap=True)
        tbl.add_column("Max Bid",    style="bold green", width=10, no_wrap=True)
        tbl.add_column("Score",      style="magenta", width=6, no_wrap=True)

        for i, rec in enumerate(recommendations, 1):
            est = (
                f"${rec['estimate_low']:.0f}–${rec['estimate_high']:.0f}"
                if rec.get("estimate_low") and rec.get("estimate_high")
                else "–"
            )
            avg = f"${rec['market_avg']:.0f}" if rec.get("market_avg") else "no data"
            bid = f"${rec['max_bid']:.0f}" if rec.get("max_bid") else "–"
            tbl.add_row(
                str(i),
                str(rec.get("lot_number") or "–"),
                _truncate(rec.get("wine_name", ""), 40),
                str(rec.get("vintage") or "NV"),
                est,
                avg,
                bid,
                f"{rec['score']:.2f}",
            )
        console.print(tbl)

        # Detail cards
        console.print()
        for i, rec in enumerate(recommendations, 1):
            self._render_rec_card(console, i, rec)

        # Strategy summary
        console.print()
        console.print(
            Panel(
                self._strategy_summary(),
                title="[bold]Active Strategy Settings[/bold]",
                border_style="dim",
            )
        )

    def _render_rec_card(self, console: Console, rank: int, rec: dict) -> None:
        wine    = rec.get("wine_name", "Unknown")
        vintage = rec.get("vintage") or "NV"
        prod    = rec.get("producer", "")
        region  = rec.get("region", "")
        reasons = rec.get("reasons", [])

        est = (
            f"${rec['estimate_low']:.0f} – ${rec['estimate_high']:.0f}"
            if rec.get("estimate_low") and rec.get("estimate_high")
            else "No estimate"
        )
        avg = f"${rec['market_avg']:.0f}" if rec.get("market_avg") else "No historical data"
        bid = f"${rec['max_bid']:.0f}" if rec.get("max_bid") else "–"

        header = f"[bold cyan]#{rank}[/bold cyan]  {wine} {vintage}"
        body_lines = [
            f"  Producer: {prod}   Region: {region}",
            f"  Estimate: {est}   Market Avg: {avg}   [bold green]Recommended max bid: {bid}[/bold green]",
            "",
        ]
        for r in reasons:
            icon = "⚠" if r.startswith("WARNING") else "✓"
            col  = "yellow" if r.startswith("WARNING") else "green"
            body_lines.append(f"  [{col}]{icon}[/{col}] {r}")

        console.print(Panel("\n".join(body_lines), title=header, border_style="cyan", padding=(0, 1)))

    def _render_post_auction(
        self,
        console: Console,
        title: str,
        prior_recs: dict,
    ) -> None:
        console.print(Panel(f"[bold blue]{title}[/bold blue]", border_style="blue"))

        # All lots with realized prices from the most recent auction
        all_results = self.db.search_lots()
        results     = [l for l in all_results if l.get("realized_price")]

        if not results:
            console.print("[yellow]No realized prices found yet. Try again later.[/yellow]")
            return

        # --- Overall stats ---
        over_est  = [l for l in results if l.get("estimate_high") and l["realized_price"] > l["estimate_high"]]
        under_est = [l for l in results if l.get("estimate_low")  and l["realized_price"] < l["estimate_low"]]
        in_range  = len(results) - len(over_est) - len(under_est)

        console.print(
            f"[bold]Results:[/bold] {len(results)} lots sold   "
            f"[green]{len(over_est)} above estimate[/green]   "
            f"[dim]{in_range} in range[/dim]   "
            f"[yellow]{len(under_est)} below estimate[/yellow]\n"
        )

        # --- Results table ---
        tbl = Table(box=box.SIMPLE_HEAD, title="[bold]Auction Results[/bold]", title_style="bold white")
        tbl.add_column("Lot",      style="cyan",   width=5)
        tbl.add_column("Wine",     style="white",  min_width=28)
        tbl.add_column("Vintage",  style="yellow", width=7)
        tbl.add_column("Estimate", style="dim",    width=14)
        tbl.add_column("Realized", style="bold",   width=10)
        tbl.add_column("vs Est",   width=8)
        tbl.add_column("vs Mkt",   width=8)

        for lot in sorted(results, key=lambda x: x.get("lot_number") or ""):
            realized = lot["realized_price"]
            est_lo   = lot.get("estimate_low")
            est_hi   = lot.get("estimate_high")
            est_str  = f"${est_lo:.0f}–${est_hi:.0f}" if est_lo and est_hi else "–"

            if est_hi and realized > est_hi:
                vs_est_str = f"[green]+{((realized/est_hi)-1)*100:.0f}%[/green]"
            elif est_lo and realized < est_lo:
                vs_est_str = f"[yellow]{((realized/est_lo)-1)*100:.0f}%[/yellow]"
            else:
                vs_est_str = "[dim]in range[/dim]"

            mkt_avg = self.db.get_market_average(lot["wine_name"], lot.get("vintage"))
            if mkt_avg:
                diff = (realized / mkt_avg - 1) * 100
                col  = "green" if diff >= 0 else "red"
                vs_mkt_str = f"[{col}]{diff:+.0f}%[/{col}]"
            else:
                vs_mkt_str = "[dim]–[/dim]"

            tbl.add_row(
                str(lot.get("lot_number") or "–"),
                _truncate(lot.get("wine_name", ""), 38),
                str(lot.get("vintage") or "NV"),
                est_str,
                f"${realized:.0f}",
                vs_est_str,
                vs_mkt_str,
            )
        console.print(tbl)

        # --- Strategy accuracy (how did our picks do?) ---
        console.print()
        self._render_strategy_accuracy(console, results, prior_recs)

        # --- Top performers ---
        console.print()
        top = sorted(
            [l for l in results if l.get("estimate_high")],
            key=lambda x: x["realized_price"] / x["estimate_high"],
            reverse=True,
        )[:5]
        if top:
            console.print("[bold]Top performers (realized / high estimate):[/bold]")
            for l in top:
                ratio = l["realized_price"] / l["estimate_high"]
                console.print(
                    f"  [green]{ratio:.1f}×[/green]  "
                    f"{l.get('wine_name','')} {l.get('vintage','')}  "
                    f"– sold ${l['realized_price']:.0f}"
                )

    def _render_strategy_accuracy(
        self, console: Console, results: list[dict], prior_recs: dict
    ) -> None:
        if not prior_recs:
            console.print("[dim]No prior recommendations to evaluate.[/dim]")
            return

        hits = misses = outbid = 0
        lines = []
        results_by_id = {r["id"]: r for r in results}

        for lot_id, rec in prior_recs.items():
            lot = results_by_id.get(lot_id)
            if lot is None:
                continue
            realized   = lot.get("realized_price")
            max_bid    = rec.get("recommended_max_bid")
            if not realized or not max_bid:
                continue
            if realized <= max_bid:
                hits += 1
                lines.append(
                    f"  [green]✓ WON[/green]  {_truncate(lot['wine_name'],30)} "
                    f"– sold ${realized:.0f} (≤ bid ${max_bid:.0f})"
                )
            else:
                outbid += 1
                lines.append(
                    f"  [yellow]✗ OUTBID[/yellow]  {_truncate(lot['wine_name'],30)} "
                    f"– sold ${realized:.0f} (bid was ${max_bid:.0f})"
                )

        total = hits + misses + outbid
        if total == 0:
            console.print("[dim]None of the recommended lots appear in this auction's results.[/dim]")
            return

        console.print(
            Panel(
                f"[bold]Strategy accuracy[/bold]  ({total} recommended lots found in results)\n"
                + f"  Winnable at recommended bid: [green]{hits}[/green]   "
                + f"Outbid: [yellow]{outbid}[/yellow]\n\n"
                + "\n".join(lines),
                border_style="blue",
            )
        )

    def _strategy_summary(self) -> str:
        b = self.cfg.get("bidding", {})
        lines = [
            f"Value threshold:    {b.get('value_threshold_pct', 15)}% below market average",
            f"Max bid:            ${b.get('max_bid_amount', 500):.0f} AUD",
            f"Preferred producers: {', '.join(b.get('preferred_producers', [])) or 'all'}",
            f"Preferred regions:   {', '.join(b.get('preferred_regions', [])) or 'all'}",
        ]
        cond = b.get("condition_requirements", {})
        if cond.get("min_fill_level"):
            lines.append(f"Min fill level:     {cond['min_fill_level']}")
        if cond.get("require_cellar_stored"):
            lines.append("Cellar storage:     required")
        if cond.get("require_original_carton"):
            lines.append("Original carton:    required")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------

    def _maybe_email(self, subject: str, body: str) -> None:
        em = self.cfg.get("schedule", {}).get("email", {})
        host = em.get("smtp_host", "")
        if not host:
            return
        to_list = em.get("to_addrs", [])
        if not to_list:
            return
        try:
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"]    = em.get("from_addr", em["smtp_user"])
            msg["To"]      = ", ".join(to_list)
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(host, em.get("smtp_port", 587)) as smtp:
                smtp.starttls()
                smtp.login(em["smtp_user"], em["smtp_password"])
                smtp.sendmail(msg["From"], to_list, msg.as_string())
            logger.info("Report emailed to %s", to_list)
        except Exception as exc:
            logger.error("Failed to send email: %s", exc)


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"
