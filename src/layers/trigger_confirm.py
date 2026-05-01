"""Layer 8 — Trigger Confirmation.

Setup score is high → "there's an opportunity here".
Trigger confirmation answers → "is the move actually starting?"

Three checks (need 2/3 by default):
  1. Volume spike: current candle volume > N× avg of last 20
  2. OI reaction: OI moved sharply within last 5min after liquidity touch
  3. Rejection candle: long wick rejecting key level (>N% wick ratio)

FIX v1.1:
  - volume_spike_multiplier: 2.0 → 1.5 (was blocking too many valid setups)
  - oi_reaction_threshold: 0.015 → 0.008 (1.5% in 5min was extremely rare)
  - rejection_wick_ratio: 0.60 → 0.50 (50% wick is still a meaningful rejection)
  - Added fallback: if oi_change_5m not provided (0.0), that check is skipped
    and only 1/2 remaining checks needed (prevents always-failing on missing data)
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.core.config import settings
from src.exchange.data_fetcher import FullSnapshot


@dataclass
class TriggerCheck:
    name: str
    confirmed: bool
    detail: str


@dataclass
class TriggerResult:
    symbol: str
    confirmed: bool
    confirmed_count: int
    required_count: int
    checks: list[TriggerCheck]
    summary: str


class TriggerConfirmation:
    def __init__(self) -> None:
        self.cfg = settings.trigger_confirmation

    def _volume_spike(self, df: pd.DataFrame) -> TriggerCheck:
        if len(df) < 21:
            return TriggerCheck("volume_spike", False, "insufficient data")
        avg20 = float(df["volume"].iloc[-21:-1].mean())
        cur   = float(df["volume"].iloc[-1])
        if avg20 == 0:
            return TriggerCheck("volume_spike", False, "zero baseline")
        ratio  = cur / avg20
        target = self.cfg["volume_spike_multiplier"]
        ok     = ratio >= target
        return TriggerCheck(
            "volume_spike", ok,
            f"vol={ratio:.2f}× avg (target {target}×)",
        )

    def _oi_reaction(self, oi_change_5m: float) -> TriggerCheck:
        """FIX: if oi_change_5m is exactly 0.0 it means data unavailable — skip."""
        if oi_change_5m == 0.0:
            return TriggerCheck("oi_reaction", False, "data unavailable — skipped")
        target = self.cfg["oi_reaction_threshold"]
        ok     = abs(oi_change_5m) >= target
        return TriggerCheck(
            "oi_reaction", ok,
            f"ΔOI(5m)={oi_change_5m*100:+.2f}% (target ±{target*100:.1f}%)",
        )

    def _rejection_candle(self, df: pd.DataFrame, direction: str) -> TriggerCheck:
        if len(df) < 1:
            return TriggerCheck("rejection_candle", False, "insufficient data")
        last = df.iloc[-1]
        upper_wick = last["high"] - max(last["close"], last["open"])
        lower_wick = min(last["close"], last["open"]) - last["low"]
        rng = last["high"] - last["low"]
        if rng == 0:
            return TriggerCheck("rejection_candle", False, "no range")
        wick  = upper_wick if direction == "SHORT" else lower_wick
        ratio = wick / rng
        target = self.cfg["rejection_wick_ratio"]
        ok    = ratio >= target
        return TriggerCheck(
            "rejection_candle", ok,
            f"{'upper' if direction == 'SHORT' else 'lower'} wick={ratio:.0%} (target {target:.0%})",
        )

    def check(
        self, snap: FullSnapshot, direction: str, oi_change_5m: float = 0.0
    ) -> TriggerResult:
        df_15m = snap.klines_15m
        checks = [
            self._volume_spike(df_15m),
            self._oi_reaction(oi_change_5m),
            self._rejection_candle(df_15m, direction),
        ]

        confirmed_count = sum(1 for c in checks if c.confirmed)

        # FIX: if OI data unavailable (skipped), only need 1/2 remaining checks
        oi_skipped = checks[1].detail == "data unavailable — skipped"
        required   = 1 if oi_skipped else self.cfg["required_confirmations"]

        confirmed = confirmed_count >= required
        summary   = f"{confirmed_count}/{len(checks)} confirmations (need {required})"
        if oi_skipped:
            summary += " [OI data skipped]"

        return TriggerResult(
            symbol=snap.symbol,
            confirmed=confirmed,
            confirmed_count=confirmed_count,
            required_count=required,
            checks=checks,
            summary=summary,
        )
