"""Layer 12 — Performance Analyzer."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from sqlalchemy import select
from src.core.database import AsyncSessionLocal, TradeOutcome

MIN_SAMPLE = 50
ROLLING_WINDOW = 50

def _decay(index: int) -> float:
    if index < 30:
        return 1.0
    if index < 50:
        return 0.6
    return 0.3

@dataclass
class CellStats:
    setup: str
    regime: str
    trades: int = 0
    wins: int = 0
    gross_profit_r: float = 0.0
    gross_loss_r: float = 0.0
    total_r: float = 0.0
    weighted_r: float = 0.0
    equity_curve: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_r(self) -> float:
        return self.total_r / self.trades if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss_r == 0:
            return 99.0 if self.gross_profit_r > 0 else 0.0
        return self.gross_profit_r / self.gross_loss_r

    @property
    def max_drawdown_r(self) -> float:
        peak, max_dd = 0.0, 0.0
        for x in self.equity_curve:
            peak = max(peak, x)
            max_dd = max(max_dd, peak - x)
        return max_dd

    def verdict(self) -> str:
        if self.trades < MIN_SAMPLE:
            return 'INSUFFICIENT_DATA'
        if self.profit_factor >= 1.8 and self.avg_r >= 0.8 and self.win_rate >= 0.60:
            return 'STRONG_EDGE'
        if self.profit_factor < 1.0 or self.avg_r < 0 or self.win_rate < 0.40:
            return 'UNDERPERFORMING'
        if self.win_rate >= 0.50 and self.avg_r >= 0.5:
            return 'VALID'
        return 'NEUTRAL'

class PerformanceAnalyzer:
    async def analyze(self, rolling_window: int = ROLLING_WINDOW) -> dict:
        async with AsyncSessionLocal() as s:
            res = await s.execute(select(TradeOutcome).order_by(TradeOutcome.closed_at.desc()).limit(rolling_window))
            outcomes = res.scalars().all()
        matrix = {}
        for idx, o in enumerate(outcomes):
            key = (o.setup_type, o.regime)
            if key not in matrix:
                matrix[key] = CellStats(setup=o.setup_type, regime=o.regime)
            cell = matrix[key]
            decay = _decay(idx)
            cell.trades += 1
            if o.result == 'WIN':
                cell.wins += 1
                cell.gross_profit_r += max(o.pnl_r, 0)
            if o.pnl_r < 0:
                cell.gross_loss_r += abs(o.pnl_r)
            cell.total_r += o.pnl_r
            cell.weighted_r += o.pnl_r * decay
            prev = cell.equity_curve[-1] if cell.equity_curve else 0.0
            cell.equity_curve.append(prev + o.pnl_r * decay)
        return matrix
