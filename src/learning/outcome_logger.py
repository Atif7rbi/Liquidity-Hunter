"""Layer 11 — Outcome Logger."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import select
from src.core.database import AsyncSessionLocal, Trade, TradeOutcome, TradeStatus
from src.core.logger import logger

class OutcomeLogger:
    async def log(
        self,
        trade: Trade,
        funding_at_entry: float = 0.0,
        ls_global_at_entry: float = 1.0,
        oi_change_4h_at_entry: float = 0.0,
    ) -> None:
        if trade.status not in (TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value):
            return
        pnl_r = trade.pnl_r or 0.0
        result = 'WIN' if pnl_r > 0 else ('BREAKEVEN' if pnl_r == 0 else 'LOSS')
        duration = 0
        if trade.triggered_at and trade.closed_at:
            duration = int((trade.closed_at - trade.triggered_at).total_seconds() / 60)
        async with AsyncSessionLocal() as s:
            existing = await s.execute(select(TradeOutcome).where(TradeOutcome.trade_id == trade.id))
            if existing.scalars().first():
                return
            row = TradeOutcome(
                trade_id=trade.id,
                symbol=trade.symbol,
                setup_type=trade.market_state,
                regime=trade.market_regime,
                direction=trade.direction,
                setup_score=trade.setup_score or 0.0,
                funding_at_entry=funding_at_entry,
                ls_global_at_entry=ls_global_at_entry,
                oi_change_4h_at_entry=oi_change_4h_at_entry,
                result=result,
                pnl_r=pnl_r,
                duration_minutes=duration,
                closed_at=trade.closed_at or datetime.utcnow(),
            )
            s.add(row)
            await s.commit()
        logger.info(f"📝 Outcome #{trade.id}: {result} {pnl_r:+.2f}R [{trade.market_state} × {trade.market_regime}]")
