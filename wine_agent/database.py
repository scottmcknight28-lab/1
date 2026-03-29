"""
SQLite database layer for the wine auction agent.

Tables
------
auctions         – one row per auction scraped from Langton's
lots             – one row per lot (wine) in an auction
bid_recommendations – strategy output for lots we want to bid on
"""

import json
import sqlite3
from datetime import datetime
from typing import Optional


class AuctionDatabase:
    def __init__(self, db_path: str = "wine_auction.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        # Step 1: create tables and non-unique indexes
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS auctions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_id  TEXT    UNIQUE NOT NULL,
                title       TEXT,
                auction_date TEXT,
                url         TEXT,
                scraped_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS lots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_id      TEXT    NOT NULL,
                lot_number      TEXT,
                wine_name       TEXT    NOT NULL,
                producer        TEXT,
                vintage         INTEGER,
                region          TEXT,
                varietal        TEXT,
                bottle_count    INTEGER DEFAULT 1,
                bottle_size     TEXT    DEFAULT '750ml',
                estimate_low    REAL,
                estimate_high   REAL,
                realized_price  REAL,
                condition_notes TEXT,
                fill_level      TEXT,
                cellar_stored   INTEGER DEFAULT 0,
                original_carton INTEGER DEFAULT 0,
                provenance      TEXT,
                lot_url         TEXT,
                FOREIGN KEY (auction_id) REFERENCES auctions(auction_id)
            );

            CREATE TABLE IF NOT EXISTS bid_recommendations (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                lot_id              INTEGER NOT NULL,
                recommended_max_bid REAL,
                value_score         REAL,
                reasons             TEXT,
                created_at          TEXT,
                FOREIGN KEY (lot_id) REFERENCES lots(id)
            );

            CREATE INDEX IF NOT EXISTS idx_lots_auction  ON lots(auction_id);
            CREATE INDEX IF NOT EXISTS idx_lots_producer ON lots(producer);
            CREATE INDEX IF NOT EXISTS idx_lots_region   ON lots(region);
            CREATE INDEX IF NOT EXISTS idx_lots_wine     ON lots(wine_name);
            CREATE INDEX IF NOT EXISTS idx_lots_vintage  ON lots(vintage);
        """)

        # Step 2: remove duplicate lot_url rows before adding unique index
        # (migration for existing databases that used plain INSERT)
        self.conn.executescript("""
            DELETE FROM lots
            WHERE id NOT IN (
                SELECT MIN(id) FROM lots
                WHERE lot_url IS NOT NULL AND lot_url != ''
                GROUP BY lot_url
            ) AND lot_url IS NOT NULL AND lot_url != '';
        """)

        # Step 3: unique index on lot_url (partial – only non-empty values)
        self.conn.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_lots_url
                ON lots(lot_url) WHERE lot_url IS NOT NULL AND lot_url != '';
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Auctions
    # ------------------------------------------------------------------

    def upsert_auction(self, auction: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO auctions (auction_id, title, auction_date, url, scraped_at)
            VALUES (:auction_id, :title, :auction_date, :url, :scraped_at)
            ON CONFLICT(auction_id) DO UPDATE SET
                title        = excluded.title,
                auction_date = excluded.auction_date,
                url          = excluded.url,
                scraped_at   = excluded.scraped_at
            """,
            auction,
        )
        self.conn.commit()

    def list_auctions(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM auctions ORDER BY auction_date DESC, scraped_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Lots
    # ------------------------------------------------------------------

    def upsert_lot(self, lot: dict) -> int:
        lot_url = (lot.get("lot_url") or "").strip()

        if lot_url:
            existing = self.conn.execute(
                "SELECT id FROM lots WHERE lot_url = ?", (lot_url,)
            ).fetchone()
            if existing:
                self.conn.execute(
                    """
                    UPDATE lots SET
                        auction_id      = :auction_id,
                        lot_number      = :lot_number,
                        wine_name       = :wine_name,
                        producer        = :producer,
                        vintage         = :vintage,
                        region          = :region,
                        varietal        = :varietal,
                        bottle_count    = :bottle_count,
                        bottle_size     = :bottle_size,
                        estimate_low    = :estimate_low,
                        estimate_high   = :estimate_high,
                        realized_price  = COALESCE(:realized_price, realized_price),
                        condition_notes = :condition_notes,
                        fill_level      = :fill_level,
                        cellar_stored   = :cellar_stored,
                        original_carton = :original_carton,
                        provenance      = :provenance
                    WHERE lot_url = :lot_url
                    """,
                    lot,
                )
                self.conn.commit()
                return existing[0]

        cur = self.conn.execute(
            """
            INSERT INTO lots (
                auction_id, lot_number, wine_name, producer, vintage, region,
                varietal, bottle_count, bottle_size,
                estimate_low, estimate_high, realized_price,
                condition_notes, fill_level, cellar_stored, original_carton,
                provenance, lot_url
            ) VALUES (
                :auction_id, :lot_number, :wine_name, :producer, :vintage, :region,
                :varietal, :bottle_count, :bottle_size,
                :estimate_low, :estimate_high, :realized_price,
                :condition_notes, :fill_level, :cellar_stored, :original_carton,
                :provenance, :lot_url
            )
            """,
            lot,
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def search_lots(
        self,
        producer: Optional[str] = None,
        region: Optional[str] = None,
        wine_name: Optional[str] = None,
        min_vintage: Optional[int] = None,
        max_vintage: Optional[int] = None,
        auction_id: Optional[str] = None,
        current_only: bool = False,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list = []

        if producer:
            conditions.append("LOWER(l.producer) LIKE LOWER(?)")
            params.append(f"%{producer}%")
        if region:
            conditions.append("LOWER(l.region) LIKE LOWER(?)")
            params.append(f"%{region}%")
        if wine_name:
            conditions.append("LOWER(l.wine_name) LIKE LOWER(?)")
            params.append(f"%{wine_name}%")
        if min_vintage is not None:
            conditions.append("l.vintage >= ?")
            params.append(min_vintage)
        if max_vintage is not None:
            conditions.append("l.vintage <= ?")
            params.append(max_vintage)
        if auction_id:
            conditions.append("l.auction_id = ?")
            params.append(auction_id)
        if current_only:
            conditions.append("l.realized_price IS NULL")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur = self.conn.execute(
            f"""
            SELECT l.*, a.title AS auction_title, a.auction_date
            FROM   lots l
            JOIN   auctions a ON l.auction_id = a.auction_id
            {where}
            ORDER  BY a.auction_date DESC, CAST(l.lot_number AS INTEGER)
            LIMIT  200
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]

    def get_lot_by_id(self, lot_id: int) -> Optional[dict]:
        cur = self.conn.execute(
            """
            SELECT l.*, a.title AS auction_title, a.auction_date
            FROM   lots l
            JOIN   auctions a ON l.auction_id = a.auction_id
            WHERE  l.id = ?
            """,
            (lot_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Market analytics
    # ------------------------------------------------------------------

    def get_market_average(
        self, wine_name: str, vintage: Optional[int] = None
    ) -> Optional[float]:
        """Return the mean realized price for a wine (optionally filtered by vintage)."""
        if vintage:
            cur = self.conn.execute(
                """
                SELECT AVG(realized_price) FROM lots
                WHERE  LOWER(wine_name) LIKE LOWER(?) AND vintage = ?
                  AND  realized_price IS NOT NULL AND realized_price > 0
                """,
                (f"%{wine_name}%", vintage),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT AVG(realized_price) FROM lots
                WHERE  LOWER(wine_name) LIKE LOWER(?)
                  AND  realized_price IS NOT NULL AND realized_price > 0
                """,
                (f"%{wine_name}%",),
            )
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    def get_price_history(self, wine_name: str, vintage: Optional[int] = None) -> list[dict]:
        """Return all realized prices for a wine, newest first."""
        params: list = [f"%{wine_name}%"]
        extra = ""
        if vintage:
            extra = "AND l.vintage = ?"
            params.append(vintage)
        cur = self.conn.execute(
            f"""
            SELECT l.wine_name, l.vintage, l.realized_price,
                   a.title AS auction_title, a.auction_date
            FROM   lots l
            JOIN   auctions a ON l.auction_id = a.auction_id
            WHERE  LOWER(l.wine_name) LIKE LOWER(?) {extra}
              AND  l.realized_price IS NOT NULL
            ORDER  BY a.auction_date DESC
            LIMIT  50
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Recommendations
    # ------------------------------------------------------------------

    def save_recommendation(
        self,
        lot_id: int,
        max_bid: float,
        score: float,
        reasons: list[str],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO bid_recommendations
                (lot_id, recommended_max_bid, value_score, reasons, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (lot_id, max_bid, score, json.dumps(reasons), datetime.now().isoformat()),
        )
        self.conn.commit()

    def get_recommendations(self) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT r.*, l.wine_name, l.vintage, l.producer, l.region,
                   l.lot_number, l.estimate_low, l.estimate_high,
                   a.title AS auction_title, a.auction_date
            FROM   bid_recommendations r
            JOIN   lots l    ON r.lot_id    = l.id
            JOIN   auctions a ON l.auction_id = a.auction_id
            ORDER  BY r.value_score DESC
            """
        )
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["reasons"] = json.loads(d.get("reasons") or "[]")
            rows.append(d)
        return rows

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        def scalar(sql: str, params: tuple = ()) -> int:
            return self.conn.execute(sql, params).fetchone()[0] or 0

        top_producers = [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT producer,
                       COUNT(*)              AS lot_count,
                       AVG(realized_price)   AS avg_realized,
                       MIN(realized_price)   AS min_realized,
                       MAX(realized_price)   AS max_realized
                FROM   lots
                WHERE  realized_price IS NOT NULL AND producer IS NOT NULL
                GROUP  BY producer
                ORDER  BY lot_count DESC
                LIMIT  15
                """
            ).fetchall()
        ]

        return {
            "total_auctions": scalar("SELECT COUNT(*) FROM auctions"),
            "total_lots": scalar("SELECT COUNT(*) FROM lots"),
            "lots_with_results": scalar(
                "SELECT COUNT(*) FROM lots WHERE realized_price IS NOT NULL"
            ),
            "active_lots": scalar(
                "SELECT COUNT(*) FROM lots WHERE realized_price IS NULL"
            ),
            "total_recommendations": scalar("SELECT COUNT(*) FROM bid_recommendations"),
            "top_producers": top_producers,
        }

    # ------------------------------------------------------------------

    def close(self) -> None:
        self.conn.close()
