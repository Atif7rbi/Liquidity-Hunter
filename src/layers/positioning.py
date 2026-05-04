"""Layer 4 — Positioning Analyzer.

Classifies the symbol into one of 6 market states based on:
  - Funding rate sign + magnitude
  - L/S ratio (global vs top-trader divergence)
  - OI behavior (building vs unwinding)

States:
  CROWDED_LONG_TRAP     → Funding+, LS>>1, OI rising → longs over-extended
  SHORT_SQUEEZE_SETUP   → Funding-, LS<<1, OI rising → shorts loaded
  SMART_MONEY_DIVERGENCE→ retail LS opposite to top-trader LS
  EXHAUSTION            → extreme funding both sides + OI dropping
  ACCUMULATION          → funding neutral, OI building, range price
  NO_SETUP              → none of the above

FIX v1.1:
  - oi_rising threshold lowered from 0.05 → 0.02 (was too strict, blocking valid setups)
  - oi_falling threshold lowered from -0.05 → -0.02 (symmetric fix)
  - CROWDED_LONG_TRAP now also triggers without oi_rising if funding+L/S extremity is very high
  - SHORT_SQUEEZE_SETUP same relaxation
  - ACCUMULATION range widened for ls_g check (0.7-1.4 → 0.6-1.5)
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.config import settings
from src.core.database import MarketState
from src.core.logger import logger
from src.exchange.data_fetcher import FullSnapshot


@dataclass
class PositioningResult:
    symbol: str
    state: MarketState
    confidence: float           # 0..1
    description: str
    bias_direction: str         # "BULLISH" / "BEARISH" / "NEUTRAL"


class PositioningAnalyzer:
    def __init__(self) -> None:
        self.cfg = settings.positioning

    def analyze(self, snap: FullSnapshot, oi_change_4h_pct: float = 0.0) -> PositioningResult:
        funding = snap.funding_rate
        ls_g = snap.ls_ratio_global
        ls_t = snap.ls_ratio_top
        
        # FIX: relaxed thresholds — was 0.05/-0.05, now 0.02/-0.02
        oi_rising  = oi_change_4h_pct >  0.02   # +2% in 4h (was +5%)
        oi_flat    = -0.02 <= oi_change_4h_pct <= 0.02
        oi_falling = oi_change_4h_pct < -0.02   # -2% (was -5%)

        f_high = self.cfg["funding_high_threshold"]
        f_low  = self.cfg["funding_low_threshold"]
        ls_long_crowded  = self.cfg["ls_crowded_long"]
        ls_short_crowded = self.cfg["ls_crowded_short"]

        # 1. Smart money divergence (highest priority — rare and powerful)
        if (ls_g > 1.5 and ls_t < 0.8) or (ls_g < 0.7 and ls_t > 1.3):
            bias = "BEARISH" if ls_g > ls_t else "BULLISH"
            return PositioningResult(
                symbol=snap.symbol,
                state=MarketState.SMART_MONEY_DIVERGENCE,
                confidence=0.85,
                description=f"Retail LS={ls_g:.2f} vs Top-traders LS={ls_t:.2f} — fade retail",
                bias_direction=bias,
            )

        # 2. Crowded long trap
        # FIX: allow oi_rising OR oi_flat when funding+LS extremity is very high
        is_extreme_long = funding >= f_high * 1.5 and ls_g >= ls_long_crowded * 1.1
        if funding >= f_high and ls_g >= ls_long_crowded and (oi_rising or is_extreme_long):
            return PositioningResult(
                symbol=snap.symbol,
                state=MarketState.CROWDED_LONG_TRAP,
                confidence=min(0.6 + (funding / f_high - 1) * 0.1, 0.95),
                description=(
                    f"Funding {funding*100:.3f}% | LS {ls_g:.2f} | "
                    f"OI {oi_change_4h_pct*100:+.1f}%. Longs paying to hold."
                ),
                bias_direction="BEARISH",
            )

        # 3. Short squeeze setup
        is_extreme_short = funding <= f_low * 1.5 and ls_g <= ls_short_crowded * 0.9
        if funding <= f_low and ls_g <= ls_short_crowded and (oi_rising or is_extreme_short):
            return PositioningResult(
                symbol=snap.symbol,
                state=MarketState.SHORT_SQUEEZE_SETUP,
                confidence=min(0.6 + (abs(funding) / abs(f_low) - 1) * 0.1, 0.95),
                description=(
                    f"Funding {funding*100:.3f}% | LS {ls_g:.2f} | "
                    f"OI {oi_change_4h_pct*100:+.1f}%. Shorts loaded."
                ),
                bias_direction="BULLISH",
            )

        # 4. Exhaustion (extreme funding + OI unwinding)
        if abs(funding) >= f_high * 1.5 and oi_falling:
            bias = "BEARISH" if funding > 0 else "BULLISH"
            return PositioningResult(
                symbol=snap.symbol,
                state=MarketState.EXHAUSTION,
                confidence=0.7,
                description=f"Extreme funding {funding*100:.3f}% + OI dropping → unwinding phase",
                bias_direction=bias,
            )

        # 5. Accumulation (calm funding, OI rising — smart money building)
        # FIX: widened ls_g range from (0.7-1.4) to (0.6-1.5) to catch more setups
        if abs(funding) < f_high * 0.3 and oi_rising and 0.6 < ls_g < 1.5:
            return PositioningResult(
                symbol=snap.symbol,
                state=MarketState.ACCUMULATION,
                confidence=0.55,
                description=(
                    f"Calm funding, OI building {oi_change_4h_pct*100:+.1f}% — accumulation phase"
                ),
                bias_direction="NEUTRAL",
            )

        # 6. Default
        return PositioningResult(
            symbol=snap.symbol,
            state=MarketState.NO_SETUP,
            confidence=0.0,
            description="No clear positioning signal",
            bias_direction="NEUTRAL",
        )
