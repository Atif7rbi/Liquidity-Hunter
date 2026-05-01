"""Layer 7 — Decision Engine v1.2

Score bands:
  ≥ min_score_full_size       → EXECUTE full size
  ≥ min_score_to_signal       → EXECUTE half size
  45 – min_score_to_signal    → WATCH (record, no execution)
  < 45 or direction==WAIT     → IGNORE
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from src.core.config import settings
from src.core.database import AsyncSessionLocal, MarketRegime, MarketState, WatchZone
from src.core.logger import logger
from src.exchange.data_fetcher import FullSnapshot
from src.layers.context import ContextResult
from src.layers.liquidity_engine import LiquidityMap
from src.layers.positioning import PositioningResult
from src.layers.regime_detector import RegimeResult

WATCH_ZONE_MIN = 45.0


@dataclass
class DecisionResult:
    symbol:        str
    score:         float
    raw_score:     float
    direction:     str
    size_factor:   float
    components:    dict[str, float] = field(default_factory=dict)
    reasoning:     list[str]        = field(default_factory=list)
    is_watch_zone: bool             = False


class DecisionEngine:
    def __init__(self) -> None:
        self.cfg     = settings.decision_engine
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
        magnitude = min(abs(oi_change_4h) / 0.15, 1.0) * 100
        if pos.state == MarketState.CROWDED_LONG_TRAP   and oi_change_4h > 0: return magnitude
        if pos.state == MarketState.SHORT_SQUEEZE_SETUP and oi_change_4h > 0: return magnitude
        if pos.state == MarketState.EXHAUSTION          and oi_change_4h < 0: return magnitude
        if pos.state == MarketState.ACCUMULATION        and oi_change_4h > 0: return magnitude * 0.7
        if pos.state == MarketState.SMART_MONEY_DIVERGENCE:                   return magnitude * 0.8
        return magnitude * 0.5

    def _funding_extreme_score(self, snap: FullSnapshot) -> float:
        f_thresh  = settings.scanner["funding_extreme_threshold"]
        magnitude = abs(snap.funding_rate) / f_thresh
        return min(magnitude * 50, 100.0)

    def _price_action_score(self, ctx: ContextResult, lmap: LiquidityMap) -> float:
        score = 0.0
        if ctx.is_recently_swept: score += 50
        if ctx.is_extended:       score += 30
        if lmap.primary_target and lmap.primary_target.distance_pct < 0.015: score += 20
        return min(score, 100.0)

    def _determine_direction(self, lmap: LiquidityMap, pos: PositioningResult) -> str:
        if pos.bias_direction == "BULLISH": return "LONG"
        if pos.bias_direction == "BEARISH": return "SHORT"
        if pos.state == MarketState.ACCUMULATION:
            return "LONG" if lmap.dominant_side == "BELOW" else "SHORT"
        if lmap.dominant_side == "ABOVE": return "SHORT"
        if lmap.dominant_side == "BELOW": return "LONG"
        return "WAIT"

    def _apply_regime_adjustment(
        self, raw_score: float, direction: str,
        pos: PositioningResult, regime: RegimeResult,
    ) -> tuple[float, list[str]]:
        notes: list[str] = []
        is_reversal = pos.state in (
            MarketState.CROWDED_LONG_TRAP, MarketState.SHORT_SQUEEZE_SETUP,
            MarketState.EXHAUSTION, MarketState.SMART_MONEY_DIVERGENCE,
        )
        score = raw_score
        if regime.regime == MarketRegime.TRENDING_UP and direction == "SHORT" and is_reversal:
            p = self.cfg["trending_market_penalty_on_reversal"]
            score *= (1 - p); notes.append(f"-{p*100:.0f}% (counter-trend in uptrend)")
        elif regime.regime == MarketRegime.TRENDING_DOWN and direction == "LONG" and is_reversal:
            p = self.cfg["trending_market_penalty_on_reversal"]
            score *= (1 - p); notes.append(f"-{p*100:.0f}% (counter-trend in downtrend)")
        elif regime.regime == MarketRegime.RANGING and is_reversal:
            b = self.cfg["range_market_bonus_on_reversal"]
            score *= (1 + b); notes.append(f"+{b*100:.0f}% (reversal play in range)")
        return min(score, 100.0), notes

    async def _save_watch_zone(
        self, result: DecisionResult, snap: FullSnapshot, regime_str: str
    ) -> None:
        try:
            async with AsyncSessionLocal() as s:
                s.add(WatchZone(
                    symbol=result.symbol, score=result.score,
                    direction=result.direction,
                    market_state=result.components.get("_state", "UNKNOWN"),
                    regime=regime_str, funding_rate=snap.funding_rate,
                    components={k: v for k, v in result.components.items() if not k.startswith("_")},
                ))
                await s.commit()
        except Exception as e:
            logger.warning(f"WatchZone persist failed for {result.symbol}: {e}")

    def evaluate(
        self, snap: FullSnapshot, lmap: LiquidityMap,
        pos: PositioningResult, ctx: ContextResult,
        regime: RegimeResult, oi_change_4h: float,
    ) -> DecisionResult:
        comp = {
            "liquidity_imbalance":     self._liquidity_imbalance_score(lmap),
            "positioning_extremity":   self._positioning_extremity_score(pos),
            "oi_behavior":             self._oi_behavior_score(snap, oi_change_4h, pos),
            "funding_extreme":         self._funding_extreme_score(snap),
            "price_action_confluence": self._price_action_score(ctx, lmap),
        }

        raw_score = sum(comp[k] * self.weights[k] for k in comp)
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
        score  = min(score, 100.0)
        if ctx.contextual_modifier != 1.0:
            reasoning.append(f"Context modifier: ×{ctx.contextual_modifier:.2f}")

        min_signal    = self.cfg["min_score_to_signal"]
        min_full      = self.cfg["min_score_full_size"]
        is_watch_zone = False

        if score < WATCH_ZONE_MIN or direction == "WAIT":
            size_factor = 0.0
            direction   = "WAIT"
        elif score < min_signal:
            # Watch zone — log to DB asynchronously
            is_watch_zone = True
            comp["_state"] = pos.state.value
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._save_watch_zone(
                    DecisionResult(
                        symbol=snap.symbol, score=score, raw_score=raw_score,
                        direction=direction, size_factor=0.0,
                        components=dict(comp), reasoning=reasoning, is_watch_zone=True,
                    ),
                    snap, regime.regime.value,
                ))
            except RuntimeError:
                pass
            size_factor = 0.0
            direction   = "WAIT"
        elif score < min_full:
            size_factor = 0.5
        else:
            size_factor = 1.0

        comp.pop("_state", None)

        return DecisionResult(
            symbol=snap.symbol, score=score, raw_score=raw_score,
            direction=direction, size_factor=size_factor,
            components=comp, reasoning=reasoning, is_watch_zone=is_watch_zone,
        )
