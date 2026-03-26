"""
Bidding strategy engine.

Scores each lot on three dimensions (weighted sum, 0–1):

  1. Producer / region match  (0.30)
  2. Price vs market value    (0.40)
  3. Provenance / condition   (0.30)

A lot is recommended for bidding when:
  - score ≥ 0.40 (configurable via min_score)
  - at least one preferred producer OR region matches (if lists are set)
  - estimated price is within the user's max_bid_amount
"""

import re
from typing import Optional

import yaml

from .scraper import FILL_LEVELS


class BiddingStrategy:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        b = cfg.get("bidding", {})
        self.value_threshold: float = b.get("value_threshold_pct", 15) / 100.0
        self.max_bid: float = float(b.get("max_bid_amount", 500))
        self.preferred_producers: list[str] = [
            p.lower() for p in b.get("preferred_producers", [])
        ]
        self.preferred_regions: list[str] = [
            r.lower() for r in b.get("preferred_regions", [])
        ]
        cond = b.get("condition_requirements", {})
        self.min_fill: str = (cond.get("min_fill_level") or "").lower()
        self.require_cellar: bool = bool(cond.get("require_cellar_stored", False))
        self.require_oc: bool = bool(cond.get("require_original_carton", False))

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def evaluate(self, lot: dict, market_avg: Optional[float]) -> dict:
        """
        Evaluate a lot and return a result dict:
            score       float 0-1
            bid         bool
            max_bid     float | None  – recommended ceiling
            reasons     list[str]     – human-readable explanation
        """
        reasons: list[str] = []
        score = 0.0

        # --- 1. Producer / Region filter ---
        prod_match = self._matches(lot.get("producer", ""), self.preferred_producers)
        reg_match  = self._matches(lot.get("region", ""), self.preferred_regions)

        if self.preferred_producers or self.preferred_regions:
            if not prod_match and not reg_match:
                return {
                    "score": 0.0,
                    "bid": False,
                    "max_bid": None,
                    "reasons": [
                        "Filtered out: not in preferred producer or region list"
                    ],
                }

        if prod_match:
            score += 0.20
            reasons.append(f"Preferred producer: {lot.get('producer')}")
        if reg_match:
            score += 0.10
            reasons.append(f"Preferred region: {lot.get('region')}")

        # --- 2. Price vs market value ---
        est_mid = self._estimate_midpoint(lot)
        price_score, price_reasons, recommended_bid = self._score_price(
            est_mid, market_avg
        )
        score += price_score
        reasons.extend(price_reasons)

        # --- 3. Provenance / condition ---
        cond_score, cond_reasons, cond_penalty = self._score_condition(lot)
        score += cond_score
        reasons.extend(cond_reasons)
        score = max(0.0, score + cond_penalty)

        # --- Decision ---
        score = round(min(score, 1.0), 3)
        within_budget = recommended_bid is not None and recommended_bid <= self.max_bid
        bid = score >= 0.40 and within_budget

        if recommended_bid and recommended_bid > self.max_bid:
            reasons.append(
                f"Note: recommended bid ${recommended_bid:.0f} exceeds your max "
                f"${self.max_bid:.0f} – skipping"
            )
            bid = False

        return {
            "score": score,
            "bid": bid,
            "max_bid": round(min(recommended_bid, self.max_bid), 2)
            if recommended_bid
            else None,
            "reasons": reasons,
        }

    def bulk_evaluate(
        self,
        lots: list[dict],
        get_market_avg,  # callable(wine_name, vintage) -> Optional[float]
        min_score: float = 0.40,
    ) -> list[dict]:
        """Evaluate a list of lots and return those meeting min_score, sorted best-first."""
        results = []
        for lot in lots:
            avg = get_market_avg(lot.get("wine_name", ""), lot.get("vintage"))
            ev  = self.evaluate(lot, avg)
            if ev["bid"] and ev["score"] >= min_score:
                results.append(
                    {
                        "lot_id":       lot.get("id"),
                        "lot_number":   lot.get("lot_number"),
                        "wine_name":    lot.get("wine_name"),
                        "vintage":      lot.get("vintage"),
                        "producer":     lot.get("producer"),
                        "region":       lot.get("region"),
                        "estimate_low": lot.get("estimate_low"),
                        "estimate_high":lot.get("estimate_high"),
                        "market_avg":   avg,
                        **ev,
                    }
                )
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(value: str, preferences: list[str]) -> bool:
        if not preferences:
            return False  # empty list → no match (caller handles "no filter" case)
        vl = value.lower()
        return any(p in vl or vl in p for p in preferences)

    @staticmethod
    def _estimate_midpoint(lot: dict) -> Optional[float]:
        lo = lot.get("estimate_low")
        hi = lot.get("estimate_high")
        if lo and hi:
            return (lo + hi) / 2
        return lo or hi or None

    def _score_price(
        self, est_mid: Optional[float], market_avg: Optional[float]
    ) -> tuple[float, list[str], Optional[float]]:
        reasons: list[str] = []
        score = 0.0
        recommended_bid: Optional[float] = None

        if market_avg and est_mid:
            discount = (market_avg - est_mid) / market_avg
            if discount >= self.value_threshold:
                score = 0.40
                recommended_bid = market_avg * (1 - self.value_threshold * 0.5)
                reasons.append(
                    f"Strong value: estimate ${est_mid:.0f} vs market avg "
                    f"${market_avg:.0f} ({discount*100:.1f}% discount)"
                )
            elif discount > 0:
                score = 0.20
                recommended_bid = est_mid * 1.05
                reasons.append(
                    f"Slight discount: estimate ${est_mid:.0f} vs market avg "
                    f"${market_avg:.0f} ({discount*100:.1f}%)"
                )
            else:
                score = 0.05
                recommended_bid = est_mid
                reasons.append(
                    f"At or above market: estimate ${est_mid:.0f} vs market avg "
                    f"${market_avg:.0f}"
                )
        elif est_mid:
            # No historical data yet
            if est_mid <= self.max_bid:
                score = 0.15
                recommended_bid = est_mid * 1.02
                reasons.append(
                    f"No market history; estimate ${est_mid:.0f} within budget"
                )
            else:
                reasons.append(
                    f"No market history; estimate ${est_mid:.0f} exceeds max bid"
                )
        else:
            reasons.append("No estimate or market data available")

        return score, reasons, recommended_bid

    def _score_condition(self, lot: dict) -> tuple[float, list[str], float]:
        reasons: list[str] = []
        score = 0.0
        penalty = 0.0

        fill = (lot.get("fill_level") or "").lower()
        if fill in FILL_LEVELS:
            idx = FILL_LEVELS.index(fill)
            # Best fill (idx 0) → 0.15; worst (idx 5) → 0.0
            fill_score = max(0.0, 0.15 - idx * 0.03)
            score += fill_score
            if fill_score >= 0.12:
                reasons.append(f"Excellent fill: {fill}")
            elif fill_score > 0:
                reasons.append(f"Acceptable fill: {fill}")

        if lot.get("cellar_stored"):
            score += 0.08
            reasons.append("Cellar-stored provenance")

        if lot.get("original_carton"):
            score += 0.07
            reasons.append("Original carton")

        # Hard requirements
        if self.require_cellar and not lot.get("cellar_stored"):
            penalty -= 0.30
            reasons.append("WARNING: cellar storage required but not stated")

        if self.require_oc and not lot.get("original_carton"):
            penalty -= 0.15
            reasons.append("WARNING: original carton required but not stated")

        if self.min_fill and fill:
            if fill in FILL_LEVELS and self.min_fill in FILL_LEVELS:
                if FILL_LEVELS.index(fill) > FILL_LEVELS.index(self.min_fill):
                    penalty -= 0.20
                    reasons.append(
                        f"WARNING: fill {fill!r} is below minimum {self.min_fill!r}"
                    )

        return score, reasons, penalty
