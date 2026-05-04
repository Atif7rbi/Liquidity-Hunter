"""Layer 8 — Trigger Confirmation.

Setup score is high → "there's an opportunity here".
Trigger confirmation answers → "is the move actually starting?"

Four checks (need 2/4 by default):
  1. Volume spike      : current candle volume > N× avg of last 20
  2. OI reaction       : OI moved sharply within last 5min after liquidity touch
  3. Rejection candle  : long wick rejecting key level (>N% wick ratio)
  4. CVD divergence    : taker flow contradicts price direction → move is weak

FIX v1.1:
  - volume_spike_multiplier: 2.0 → 1.5
  - oi_reaction_threshold: 0.015 → 0.008
  - rejection_wick_ratio: 0.60 → 0.50
  - Added fallback: if oi_change_5m not provided, that check is skipped

FIX v1.2:
  - Added CVD divergence check (check #4)
    Uses snap.taker_buy_volume / snap.taker_sell_volume
    Divergence = price moved in direction but taker flow is opposite
    If CVD data unavailable (ratio == 1.0), check is skipped like OI
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
        wick   = upper_wick if direction == "SHORT" else lower_wick
        ratio  = wick / rng
        target = self.cfg["rejection_wick_ratio"]
        ok     = ratio >= target
        return TriggerCheck(
            "rejection_candle", ok,
            f"{'upper' if direction == 'SHORT' else 'lower'} wick={ratio:.0%} (target {target:.0%})",
        )

    def _cvd_divergence(self, snap: FullSnapshot, direction: str) -> TriggerCheck:
        """
        CVD Divergence check (v1.2).

        taker_buy_volume  = buySellRatio from Binance (buy / sell)
        taker_sell_volume = 1 / buySellRatio (computed in data_fetcher)

        Divergence logic:
          - Signal is LONG  but sellers dominate (ratio < 1.0) → weak move → confirmed
          - Signal is SHORT but buyers  dominate (ratio > 1.0) → weak move → confirmed

        If ratio == 1.0 (default / unavailable) → skip.
        """
        buy_vol  = snap.taker_buy_volume   # buySellRatio
        sell_vol = snap.taker_sell_volume  # 1 / buySellRatio

        # data unavailable — both default to 1.0 in data_fetcher
        if buy_vol == 1.0 and sell_vol == 1.0:
            return TriggerCheck("cvd_divergence", False, "data unavailable — skipped")

        # ratio > 1 = buyers dominant, < 1 = sellers dominant
        ratio = buy_vol  # already the buy/sell ratio

        if direction == "LONG" and ratio < 1.0:
            # Price expected to go up, but sellers dominate → divergence (bearish flow)
            ok = True
            detail = f"CVD divergence: LONG signal but sell dominance ratio={ratio:.3f} (<1.0)"
        elif direction == "SHORT" and ratio > 1.0:
            # Price expected to go down, but buyers dominate → divergence (bullish flow)
            ok = True
            detail = f"CVD divergence: SHORT signal but buy dominance ratio={ratio:.3f} (>1.0)"
        else:
            ok = False
            detail = f"No CVD divergence: ratio={ratio:.3f} aligned with {direction}"

        return TriggerCheck("cvd_divergence", ok, detail)

    def check(
        self, snap: FullSnapshot, direction: str, oi_change_5m: float = 0.0
    ) -> TriggerResult:
        df_15m = snap.klines_15m
        checks = [
            self._volume_spike(df_15m),
            self._oi_reaction(oi_change_5m),
            self._rejection_candle(df_15m, direction),
            self._cvd_divergence(snap, direction),   # NEW
        ]

        confirmed_count = sum(1 for c in checks if c.confirmed)

        # Count skipped checks
        skipped = sum(
            1 for c in checks
            if "skipped" in c.detail
        )
        available = len(checks) - skipped

        # Need required_confirmations out of available checks (min 1)
        required = max(1, min(self.cfg["required_confirmations"], available))

        confirmed = confirmed_count >= required
        summary   = f"{confirmed_count}/{len(checks)} confirmations (need {required})"
        if skipped:
            summary += f" [{skipped} skipped]"

        return TriggerResult(
            symbol=snap.symbol,
            confirmed=confirmed,
            confirmed_count=confirmed_count,
            required_count=required,
            checks=checks,
            summary=summary,
        )
