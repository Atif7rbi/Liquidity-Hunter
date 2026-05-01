"""Layer 10 — Paper Executor.

Simulates trade execution without touching real money.
  - Manages virtual portfolio (initial capital, equity curve)
  - Sizes positions (1% risk by default)
  - Models slippage, spread, missed entries
  - Enforces daily kill-switch
  - Tracks open positions and updates them on each tick

Trades flow: PENDING (waiting for trigger) → TRIGGERED (entry filled)
            → CLOSED_TP / CLOSED_SL / CANCELLED / EXPIRED
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from src.core.config import settings
from src.core.database import (
    AsyncSessionLocal,
    PortfolioSnapshot,
    Trade,
    TradeDirection,
    TradeStatus,
)
from src.core.logger import logger
from src.layers.trade_generator import TradeCard
from src.learning.outcome_logger import OutcomeLogger


class PaperExecutor:
    def __init__(self) -> None:
        self.cfg = settings.paper_executor
        self.initial_capital = self.cfg["initial_capital_usd"]
        self.risk_pct = self.cfg["risk_per_trade_pct"]
        self.slippage_entry = self.cfg["slippage_entry_pct"]
        self.slippage_stop = self.cfg["slippage_stop_pct"]
        self.spread = self.cfg["spread_pct"]
        self.fee_pct = settings.backtest["fee_pct"]

    # ---------- Portfolio ----------

    async def get_equity(self) -> float:
        """Current equity = initial + sum of closed PnL."""
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value])
                )
            )
            closed = res.scalars().all()
            realized = sum((t.pnl_usd or 0) - (t.fees_usd or 0) for t in closed)
            return self.initial_capital + realized

    async def get_open_count(self) -> int:
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value])
                )
            )
            return len(res.scalars().all())

    async def get_open_trades(self) -> list:
        """جلب الصفقات المفتوحة (PENDING + TRIGGERED) للـ UI."""
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([
                        TradeStatus.PENDING.value,
                        TradeStatus.TRIGGERED.value,
                    ])
                ).order_by(Trade.created_at.desc())
            )
            trades = res.scalars().all()

        for t in trades:
            t.entry_price = t.actual_entry_price or ((t.entry_zone_low + t.entry_zone_high) / 2)
            t.tp1 = t.take_profit_1
            t.tp2 = t.take_profit_2
            t.opened_at = t.triggered_at or t.created_at

        return trades

    async def is_kill_switch_active(self) -> bool:
        """Check daily loss / consecutive losses."""
        async with AsyncSessionLocal() as s:
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            res = await s.execute(
                select(Trade).where(
                    Trade.closed_at >= today_start,
                    Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]),
                )
            )
            today_closed = sorted(res.scalars().all(), key=lambda t: t.closed_at)

            equity = await self.get_equity()
            daily_pnl = sum((t.pnl_usd or 0) for t in today_closed)
            if equity > 0 and daily_pnl < 0:
                if abs(daily_pnl) / self.initial_capital >= self.cfg["daily_max_loss_pct"]:
                    logger.warning(f"KILL SWITCH: daily loss ${daily_pnl:.2f}")
                    return True

            # Consecutive losses (across most recent trades)
            recent = sorted(today_closed, key=lambda t: t.closed_at, reverse=True)
            consec = 0
            for t in recent:
                if (t.pnl_usd or 0) < 0:
                    consec += 1
                else:
                    break
            if consec >= self.cfg["daily_max_consecutive_losses"]:
                logger.warning(f"KILL SWITCH: {consec} consecutive losses today")
                return True

        return False

    # ---------- Position sizing ----------

    def _compute_size(self, equity: float, entry: float, sl: float, size_factor: float) -> tuple[float, float]:
        """Returns (notional_usd, risk_usd) using fixed-% risk."""
        risk_amount = equity * self.risk_pct * size_factor
        risk_per_unit = abs(entry - sl)
        if risk_per_unit == 0:
            return 0.0, 0.0
        units = risk_amount / risk_per_unit
        notional = units * entry
        # Cap notional at 20% of equity (no over-leverage even with tight stops)
        notional = min(notional, equity * 0.20)
        return notional, risk_amount

    # ---------- Trade lifecycle ----------

    async def submit(self, card: TradeCard) -> Optional[int]:
        """Persist a TradeCard as PENDING trade."""
        if await self.is_kill_switch_active():
            logger.warning(f"Kill switch active, skipping {card.symbol}")
            return None

        equity = await self.get_equity()
        if await self.get_open_count() >= self.cfg["max_concurrent_trades"]:
            logger.info(f"Max concurrent trades reached, skipping {card.symbol}")
            return None

        # Use mid of entry zone for sizing
        entry_mid = (card.entry_zone_low + card.entry_zone_high) / 2
        notional, risk = self._compute_size(equity, entry_mid, card.stop_loss, card.size_factor)
        if notional <= 0:
            return None

        async with AsyncSessionLocal() as s:
            # Dedupe: skip if a non-final trade exists for this symbol
            res = await s.execute(
                select(Trade).where(
                    Trade.symbol == card.symbol,
                    Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value]),
                )
            )
            if res.scalars().first():
                logger.info(f"{card.symbol}: trade already active, skipping")
                return None

            trade = Trade(
                symbol=card.symbol,
                direction=TradeDirection.LONG.value if card.direction == "LONG" else TradeDirection.SHORT.value,
                status=TradeStatus.PENDING.value,
                setup_score=card.setup_score,
                market_state=card.market_state.value,
                market_regime=card.market_regime.value,
                trigger_description=card.trigger_description,
                trigger_confirmed_count=0,
                invalidation_condition=card.invalidation_condition,
                entry_zone_low=card.entry_zone_low,
                entry_zone_high=card.entry_zone_high,
                stop_loss=card.stop_loss,
                take_profit_1=card.tp1,
                take_profit_2=card.tp2,
                take_profit_3=card.tp3,
                risk_reward_ratio=card.risk_reward,
                position_size_usd=notional,
                risk_amount_usd=risk,
                created_at=card.created_at,
                notes=" | ".join(card.reasoning[:5]),
            )
            s.add(trade)
            await s.commit()
            await s.refresh(trade)
            logger.info(
                f"📝 PENDING {card.direction} {card.symbol} | size=${notional:.0f} | "
                f"risk=${risk:.2f} | entry=[{card.entry_zone_low:.4g},{card.entry_zone_high:.4g}] | "
                f"SL={card.stop_loss:.4g} | TP2={card.tp2:.4g} | RR={card.risk_reward:.2f}"
            )
            return trade.id

    async def trigger(self, trade_id: int, fill_price: float, confirmed_count: int) -> None:
        """Mark a pending trade as TRIGGERED with realistic fill."""
        # Apply slippage + spread to entry
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, trade_id)
            if t is None or t.status != TradeStatus.PENDING.value:
                return
            slip = 1 + self.slippage_entry + self.spread / 2
            if t.direction == TradeDirection.LONG.value:
                actual = fill_price * slip
            else:
                actual = fill_price / slip
            t.actual_entry_price = actual
            t.status = TradeStatus.TRIGGERED.value
            t.triggered_at = datetime.utcnow()
            t.trigger_confirmed_count = confirmed_count
            # Entry fee
            t.fees_usd = (t.position_size_usd or 0) * self.fee_pct
            await s.commit()
            logger.info(f"🎯 TRIGGERED {t.symbol} @ {actual:.4g}")

    async def close(
        self, trade_id: int, exit_price: float, status: TradeStatus, reason: str = ""
    ) -> None:
        """Close a triggered trade. Computes PnL with slippage on stop, fee on exit."""
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, trade_id)
            if t is None or t.status != TradeStatus.TRIGGERED.value:
                return

            # Apply slippage on SL exits (worse fill)
            actual_exit = exit_price
            if status == TradeStatus.CLOSED_SL:
                slip = 1 + self.slippage_stop + self.spread / 2
                if t.direction == TradeDirection.LONG.value:
                    actual_exit = exit_price / slip
                else:
                    actual_exit = exit_price * slip

            entry = t.actual_entry_price or 0
            units = (t.position_size_usd or 0) / entry if entry else 0
            if t.direction == TradeDirection.LONG.value:
                pnl = (actual_exit - entry) * units
            else:
                pnl = (entry - actual_exit) * units

            risk = t.risk_amount_usd or 1
            pnl_r = pnl / risk

            t.exit_price = actual_exit
            t.pnl_usd = pnl
            t.pnl_r = pnl_r
            t.fees_usd = (t.fees_usd or 0) + (t.position_size_usd or 0) * self.fee_pct
            t.status = status.value
            t.closed_at = datetime.utcnow()
            if reason:
                t.notes = (t.notes or "") + f" | EXIT: {reason}"
            await s.commit()
            emoji = "✅" if pnl > 0 else "❌"
            logger.info(
                f"{emoji} CLOSED {t.symbol} {status.value} | exit={actual_exit:.4g} | "
                f"PnL=${pnl:+.2f} ({pnl_r:+.2f}R)"
            )
            # ── Learning Loop ──
            await OutcomeLogger().log(t)

    async def cancel(self, trade_id: int, reason: str) -> None:
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, trade_id)
            if t is None or t.status != TradeStatus.PENDING.value:
                return
            t.status = TradeStatus.CANCELLED.value
            t.closed_at = datetime.utcnow()
            t.notes = (t.notes or "") + f" | CANCELLED: {reason}"
            await s.commit()
            logger.info(f"🚫 CANCELLED {t.symbol}: {reason}")

    # ---------- Tick handler ----------

    async def update_positions(self, prices: dict[str, float]) -> None:
        """Called on each price update — checks for triggers, TPs, SLs, expirations."""
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value])
                )
            )
            active = res.scalars().all()

        max_age = timedelta(hours=8)  # PENDING expires after 8h
        now = datetime.utcnow()

        for t in active:
            price = prices.get(t.symbol)
            if price is None:
                continue

            if t.status == TradeStatus.PENDING.value:
                # ── Missed Entry Check ───────────────────────────────────
                # LONG : فات الدخول لو السعر صعد فوق الزون
                # SHORT: فات الدخول لو السعر نزل تحت الزون
                missed = self.cfg["missed_entry_max_pct"]
                if t.direction == TradeDirection.LONG.value:
                    if price > t.entry_zone_high * (1 + missed):
                        await self.cancel(t.id, f"missed entry: price {price:.4g} ran above zone {t.entry_zone_high:.4g}")
                        continue
                else:  # SHORT
                    if price < t.entry_zone_low * (1 - missed):
                        await self.cancel(t.id, f"missed entry: price {price:.4g} ran below zone {t.entry_zone_low:.4g}")
                        continue

                # ── Expiry ───────────────────────────────────────────────
                if t.created_at and (now - t.created_at) > max_age:
                    await self.cancel(t.id, "pending expired (>8h)")
                    continue

                # ── Auto-Trigger: price entered the entry zone ───────────
                # الصفقة تتفعل فور دخول السعر منطقة الدخول
                if t.entry_zone_low <= price <= t.entry_zone_high:
                    await self.trigger(t.id, price, confirmed_count=1)
                    logger.info(f"⚡ AUTO-TRIGGERED {t.symbol} @ {price:.4g} (price entered zone)")
                    continue

            elif t.status == TradeStatus.TRIGGERED.value:
                # Check TP / SL
                if t.direction == TradeDirection.LONG.value:
                    if price <= t.stop_loss:
                        await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, "SL hit")
                    elif price >= t.take_profit_2:  # exit on TP2 for simplicity
                        await self.close(t.id, t.take_profit_2, TradeStatus.CLOSED_TP, "TP2 hit")
                else:
                    if price >= t.stop_loss:
                        await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, "SL hit")
                    elif price <= t.take_profit_2:
                        await self.close(t.id, t.take_profit_2, TradeStatus.CLOSED_TP, "TP2 hit")

    async def take_portfolio_snapshot(self) -> None:
        async with AsyncSessionLocal() as s:
            equity = await self.get_equity()
            open_count = await self.get_open_count()
            kill = await self.is_kill_switch_active()
            snap = PortfolioSnapshot(
                equity_usd=equity,
                open_positions=open_count,
                daily_pnl_usd=0.0,  # filled by reporter
                daily_pnl_pct=0.0,
                consecutive_losses=0,
                kill_switch_active=kill,
            )
            s.add(snap)
            await s.commit()
