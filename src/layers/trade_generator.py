"""Layer 9 — Trade Generator.

Builds a complete TradeCard from a high-quality decision:
  - Direction
  - Conditional trigger ("after sweep above 77,500 with 1H close below")
  - Entry zone (range, not point)
  - SL behind structural invalidation
  - TP1/TP2/TP3 with exit ratios
  - R:R must be ≥ 2.0
  - Explicit invalidation condition
  - Estimated success probability from setup score
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.core.config import settings
from src.core.database import MarketRegime, MarketState
from src.core.logger import logger
from src.exchange.data_fetcher import FullSnapshot
from src.layers.decision_engine import DecisionResult
from src.layers.liquidity_engine import LiquidityMap, Zone


@dataclass
class TradeCard:
    symbol: str
    direction: str                   # "LONG" / "SHORT"
    setup_score: float
    market_state: MarketState
    market_regime: MarketRegime

    trigger_description: str
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_reward: float

    invalidation_condition: str
    estimated_success: float
    size_factor: float
    reasoning: list[str]
    created_at: datetime


class TradeGenerator:
    def __init__(self) -> None:
        self.cfg = settings.trade_generator

    def _find_invalidation(
        self, lmap: LiquidityMap, direction: str, current_price: float
    ) -> Optional[float]:
        """Structural invalidation level — beyond which the thesis is wrong.
        For SHORT: highest near-term liquidity zone above (sweep target).
        For LONG: lowest near-term liquidity zone below.
        """
        if direction == "SHORT":
            zones = [z for z in lmap.zones_above if z.distance_pct < 0.03]  # within 3%
            if not zones:
                return current_price * 1.015  # fallback 1.5% above
            return max(z.price_level for z in zones)
        else:
            zones = [z for z in lmap.zones_below if z.distance_pct < 0.03]
            if not zones:
                return current_price * 0.985
            return min(z.price_level for z in zones)

    def _find_targets(
        self, lmap: LiquidityMap, direction: str, current_price: float
    ) -> tuple[float, float, float]:
        """TP1/TP2/TP3 from opposite-side liquidity zones."""
        if direction == "SHORT":
            zones = sorted(lmap.zones_below, key=lambda z: z.distance_pct)
        else:
            zones = sorted(lmap.zones_above, key=lambda z: z.distance_pct)

        # Need 3 targets — pad with synthetic if not enough
        targets: list[float] = [z.price_level for z in zones[:3]]
        while len(targets) < 3:
            n = len(targets) + 1
            if direction == "SHORT":
                targets.append(current_price * (1 - 0.01 * n))
            else:
                targets.append(current_price * (1 + 0.01 * n))
        return targets[0], targets[1], targets[2]

    def _build_trigger(
        self, snap: FullSnapshot, lmap: LiquidityMap, direction: str
    ) -> str:
        """Compose conditional trigger description."""
        if direction == "SHORT":
            target = lmap.zones_above[0] if lmap.zones_above else None
            if target:
                return (
                    f"Sweep above {target.price_level:.4g} (≈+{target.distance_pct*100:.2f}%) "
                    f"+ 15m candle close back below {target.price_level:.4g}"
                )
            return f"15m close below {snap.price * 0.998:.4g} with volume spike"
        else:
            target = lmap.zones_below[0] if lmap.zones_below else None
            if target:
                return (
                    f"Sweep below {target.price_level:.4g} (≈-{target.distance_pct*100:.2f}%) "
                    f"+ 15m candle close back above {target.price_level:.4g}"
                )
            return f"15m close above {snap.price * 1.002:.4g} with volume spike"

    def _build_invalidation_text(
        self, invalidation_price: float, direction: str
    ) -> str:
        if direction == "SHORT":
            return (
                f"1H candle closes above {invalidation_price:.4g} → thesis dead, no entry"
            )
        return (
            f"1H candle closes below {invalidation_price:.4g} → thesis dead, no entry"
        )

    def generate(
        self,
        snap: FullSnapshot,
        lmap: LiquidityMap,
        decision: DecisionResult,
        market_state: MarketState,
        market_regime: MarketRegime,
    ) -> Optional[TradeCard]:
        if decision.direction == "WAIT":
            return None

        direction = decision.direction
        price = snap.price

        # Entry zone: a band, not a point
        zone_width = self.cfg["entry_zone_width_pct"]

        if direction == "SHORT":
            # Enter on retest of a sweep zone (slightly above current)
            sweep_target = lmap.zones_above[0].price_level if lmap.zones_above else price * 1.005
            entry_high = sweep_target
            entry_low = sweep_target * (1 - zone_width)
        else:
            sweep_target = lmap.zones_below[0].price_level if lmap.zones_below else price * 0.995
            entry_low = sweep_target
            entry_high = sweep_target * (1 + zone_width)

        entry_mid = (entry_low + entry_high) / 2

        # Invalidation = structural level
        invalidation = self._find_invalidation(lmap, direction, price)
        if invalidation is None:
            return None

        # SL = invalidation + buffer
        buffer = self.cfg["stop_buffer_pct"]
        if direction == "SHORT":
            sl = invalidation * (1 + buffer)
        else:
            sl = invalidation * (1 - buffer)

        # Targets
        tp1, tp2, tp3 = self._find_targets(lmap, direction, price)

        # Risk:reward (using TP2 as the primary measure — average target)
        if direction == "SHORT":
            risk = sl - entry_mid
            reward = entry_mid - tp2
        else:
            risk = entry_mid - sl
            reward = tp2 - entry_mid

        if risk <= 0 or reward <= 0:
            logger.warning(f"{snap.symbol}: invalid risk/reward geometry, skipping")
            return None

        rr = reward / risk
        if rr < self.cfg["min_risk_reward_ratio"]:
            logger.info(f"{snap.symbol}: R:R {rr:.2f} < min, skipping")
            return None

        # Estimated success: derive from score (60→55%, 75→65%, 90→75%)
        estimated_success = 0.40 + (decision.score / 100) * 0.40

        return TradeCard(
            symbol=snap.symbol,
            direction=direction,
            setup_score=decision.score,
            market_state=market_state,
            market_regime=market_regime,
            trigger_description=self._build_trigger(snap, lmap, direction),
            entry_zone_low=entry_low,
            entry_zone_high=entry_high,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            risk_reward=rr,
            invalidation_condition=self._build_invalidation_text(invalidation, direction),
            estimated_success=estimated_success,
            size_factor=decision.size_factor,
            reasoning=decision.reasoning,
            created_at=datetime.utcnow(),
        )
