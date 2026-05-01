"""Layer 5 — Context Layer.

Adds 24h memory: same signal means different things depending on what came before.
  - Funding+ + recent pump (>5% / 24h) → late stage, exhaustion likely
  - Funding+ + range/dip → early longs, may have room
  - OI buildup spike vs gradual buildup → fragility differs
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.config import settings
from src.exchange.data_fetcher import FullSnapshot


@dataclass
class ContextResult:
    symbol: str
    price_change_24h_pct: float
    price_change_4h_pct: float
    is_extended: bool             # > 5% in 24h, suggesting late stage
    is_recently_swept: bool       # quick wick rejection in last 4 candles
    oi_buildup_pace: str          # "GRADUAL" / "EXPLOSIVE" / "DECLINING"
    contextual_modifier: float    # multiplier on positioning score: 0.5..1.5
    notes: list[str]


class ContextLayer:
    def __init__(self) -> None:
        self.cfg = settings.context_layer

    def analyze(self, snap: FullSnapshot, oi_change_4h_pct: float = 0.0) -> ContextResult:
        df_1h = snap.klines_1h
        df_15m = snap.klines_15m

        notes: list[str] = []
        modifier = 1.0

        # 24h price change
        if len(df_1h) >= 24:
            price_24h_ago = float(df_1h["close"].iloc[-25])
            change_24h = (snap.price - price_24h_ago) / price_24h_ago
        else:
            change_24h = 0.0

        # 4h price change
        if len(df_1h) >= 4:
            price_4h_ago = float(df_1h["close"].iloc[-5])
            change_4h = (snap.price - price_4h_ago) / price_4h_ago
        else:
            change_4h = 0.0

        # Extended move flag
        is_extended = abs(change_24h) > 0.05
        if is_extended:
            notes.append(f"Price extended {change_24h*100:+.1f}% in 24h")
            # If positioning is bearish AND price already extended up → boost (mean reversion likely)
            modifier *= 1.15

        # Recent sweep detection: did last 4 candles have a long wick rejection?
        is_recently_swept = False
        if len(df_15m) >= 4:
            last4 = df_15m.tail(4)
            for _, row in last4.iterrows():
                body = abs(row["close"] - row["open"])
                upper_wick = row["high"] - max(row["close"], row["open"])
                lower_wick = min(row["close"], row["open"]) - row["low"]
                wick_max = max(upper_wick, lower_wick)
                if body > 0 and wick_max / (body + 1e-9) > 1.5:
                    is_recently_swept = True
                    break
            if is_recently_swept:
                notes.append("Recent sweep wick detected in last 4×15m bars")
                modifier *= 1.10

        # OI buildup pace
        if oi_change_4h_pct > 0.15:
            pace = "EXPLOSIVE"
            notes.append(f"OI exploded +{oi_change_4h_pct*100:.1f}%/4h — fragile")
            modifier *= 1.10
        elif oi_change_4h_pct > 0.05:
            pace = "GRADUAL"
        elif oi_change_4h_pct < -0.05:
            pace = "DECLINING"
            notes.append(f"OI declining {oi_change_4h_pct*100:.1f}%/4h — unwinding")
        else:
            pace = "GRADUAL"

        # Cap modifier
        modifier = max(0.5, min(modifier, 1.5))

        return ContextResult(
            symbol=snap.symbol,
            price_change_24h_pct=change_24h,
            price_change_4h_pct=change_4h,
            is_extended=is_extended,
            is_recently_swept=is_recently_swept,
            oi_buildup_pace=pace,
            contextual_modifier=modifier,
            notes=notes,
        )
