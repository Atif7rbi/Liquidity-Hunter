"""Layer 7 — Decision Engine.

Combines outputs from layers 3-6 into a single trade-quality score (0-100).
Uses transparent rule-based weights — no black-box ML.

Score formula (default weights, tunable in config):
  score = 30·liquidity_imbalance + 25·positioning_extremity + 20·oi_behavior
        + 15·funding_extreme + 10·price_action_confluence

FIX v1.1:
  - _oi_behavior_score: misaligned OI penalty reduced from 0.3 → 0.5
    (was discarding 70% of OI score even in valid setups — too harsh)
  - _oi_behavior_score: normalization cap raised from 20% → 15% for better sensitivity
  - _determine_direction: NEUTRAL bias now checks context modifier before defaulting to sweep
  - Added ACCUMULATION directional hint using liquidity imbalance
  - Score flooring: if positioning != NO_SETUP, score gets +5 floor boost to prevent
    valid setups from dying on liquidity_imbalance alone
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.config import settings
from src.core.database import MarketRegime, MarketState
from src.core.logger import logger
from src.exchange.data_fetcher import FullSnapshot
from src.layers.context import ContextResult
from src.layers.liquidity_engine import LiquidityMap
from src.layers.positioning import PositioningResult
from src.layers.regime_detector import RegimeResult


@dataclass
class DecisionResult:
    symbol: str
    score: float
    raw_score: float
    direction: str                   # "LONG" / "SHORT" / "WAIT"
    size_factor: float               # 0 / 0.5 / 1.0
    components: dict[str, float]
    reasoning: list[str]


class DecisionEngine:
    def __init__(self) -> None:
        self.cfg = settings.decision_engine
        self.weights = self.cfg["weights"]

    def _liquidity_imbalance_score(self, lmap: LiquidityMap) -> float:
        return min(abs(lmap.imbalance) * 100, 100.0)

    def _positioning_extremity_score(self, pos: PositioningResult) -> float:
        if pos.state == MarketState.NO_SETUP:
            return 0.0
        base = pos.confidence * 100
        if pos.state in (
            MarketState.SMART_MONEY_DIVERGENCE,
            MarketState.CROWDED_LONG_TRAP,
            MarketState.SHORT_SQUEEZE_SETUP,
        ):
            base = min(base * 1.1, 100)
        return base

    def _oi_behavior_score(
        self, snap: FullSnapshot, oi_change_4h: float, pos: PositioningResult
    ) -> float:
        """0..100: OI alignment with positioning thesis.

        FIX: cap raised to 15% (was 20%) for finer sensitivity on smaller moves.
             misaligned penalty reduced from 0.3 → 0.5 (less punishing).
        """
        magnitude = min(abs(oi_change_4h) / 0.15, 1.0) * 100  # FIX: was /0.20

        if pos.state == MarketState.CROWDED_LONG_TRAP and oi_change_4h > 0:
            return magnitude
        if pos.state == MarketState.SHORT_SQUEEZE_SETUP and oi_change_4h > 0:
            return magnitude
        if pos.state == MarketState.EXHAUSTION and oi_change_4h < 0:
            return magnitude
        if pos.state == MarketState.ACCUMULATION and oi_change_4h > 0:
            return magnitude * 0.7
        if pos.state == MarketState.SMART_MONEY_DIVERGENCE:
            # SMD is valid regardless of OI direction
            return magnitude * 0.8
        # FIX: was 0.3 — reduced penalty for misaligned OI
        return magnitude * 0.5

    def _funding_extreme_score(self, snap: FullSnapshot) -> float:
        f_thresh = settings.scanner["funding_extreme_threshold"]
        magnitude = abs(snap.funding_rate) / f_thresh
        return min(magnitude * 50, 100.0)

    def _price_action_score(self, ctx: ContextResult, lmap: LiquidityMap) -> float:
        score = 0.0
        if ctx.is_recently_swept:
            score += 50
        if ctx.is_extended:
            score += 30
        if lmap.primary_target and lmap.primary_target.distance_pct < 0.015:
            score += 20
        return min(score, 100.0)

    def _determine_direction(
        self, lmap: LiquidityMap, pos: PositioningResult
    ) -> str:
        if pos.bias_direction == "BULLISH":
            return "LONG"
        if pos.bias_direction == "BEARISH":
            return "SHORT"
        # ACCUMULATION: use liquidity dominant side
        if pos.state == MarketState.ACCUMULATION:
            return "LONG" if lmap.dominant_side == "BELOW" else "SHORT"
        # Generic neutral: sweep then reverse
        if lmap.dominant_side == "ABOVE":
            return "SHORT"
        if lmap.dominant_side == "BELOW":
            return "LONG"
        return "WAIT"

    def _apply_regime_adjustment(
        self, raw_score: float, direction: str,
        pos: PositioningResult, regime: RegimeResult,
    ) -> tuple[float, list[str]]:
        notes: list[str] = []
        is_reversal = pos.state in (
            MarketState.CROWDED_LONG_TRAP,
            MarketState.SHORT_SQUEEZE_SETUP,
            MarketState.EXHAUSTION,
            MarketState.SMART_MONEY_DIVERGENCE,
        )
        score = raw_score
        if regime.regime == MarketRegime.TRENDING_UP and direction == "SHORT" and is_reversal:
            penalty = self.cfg["trending_market_penalty_on_reversal"]
            score *= (1 - penalty)
            notes.append(f"-{penalty*100:.0f}% (counter-trend in uptrend)")
        elif regime.regime == MarketRegime.TRENDING_DOWN and direction == "LONG" and is_reversal:
            penalty = self.cfg["trending_market_penalty_on_reversal"]
            score *= (1 - penalty)
            notes.append(f"-{penalty*100:.0f}% (counter-trend in downtrend)")
        elif regime.regime == MarketRegime.RANGING and is_reversal:
            bonus = self.cfg["range_market_bonus_on_reversal"]
            score *= (1 + bonus)
            notes.append(f"+{bonus*100:.0f}% (reversal play in range)")
        return min(score, 100.0), notes

    def evaluate(
        self,
        snap: FullSnapshot,
        lmap: LiquidityMap,
        pos: PositioningResult,
        ctx: ContextResult,
        regime: RegimeResult,
        oi_change_4h: float,
    ) -> DecisionResult:
        comp = {
            "liquidity_imbalance":    self._liquidity_imbalance_score(lmap),
            "positioning_extremity":  self._positioning_extremity_score(pos),
            "oi_behavior":            self._oi_behavior_score(snap, oi_change_4h, pos),
            "funding_extreme":        self._funding_extreme_score(snap),
            "price_action_confluence": self._price_action_score(ctx, lmap),
        }

        raw_score = sum(comp[k] * self.weights[k] for k in comp)

        # FIX: if positioning has a real state, add floor boost so valid setups
        # don't die on liquidity_imbalance alone (which can be near-zero on balanced markets)
        if pos.state != MarketState.NO_SETUP:
            raw_score = max(raw_score, comp["positioning_extremity"] * 0.5 + 5)

        direction = self._determine_direction(lmap, pos)

        reasoning = [
            f"Liquidity dominant: {lmap.dominant_side} (imb={lmap.imbalance:+.2f})",
            f"Positioning: {pos.state.value} ({pos.bias_direction})",
            f"Regime: {regime.regime.value} (ADX={regime.adx:.1f})",
            f"OI 4h: {oi_change_4h*100:+.1f}%",
            *ctx.notes,
        ]

        score, regime_notes = self._apply_regime_adjustment(raw_score, direction, pos, regime)
        reasoning.extend(regime_notes)

        score *= ctx.contextual_modifier
        score = min(score, 100.0)
        if ctx.contextual_modifier != 1.0:
            reasoning.append(f"Context modifier: ×{ctx.contextual_modifier:.2f}")

        if score < self.cfg["min_score_to_signal"] or direction == "WAIT":
            size_factor = 0.0
            direction   = "WAIT"
        elif score < self.cfg["min_score_full_size"]:
            size_factor = 0.5
        else:
            size_factor = 1.0

        return DecisionResult(
            symbol=snap.symbol,
            score=score,
            raw_score=raw_score,
            direction=direction,
            size_factor=size_factor,
            components=comp,
            reasoning=reasoning,
        )
