"""Layer 10 — Paper Executor (v3 — Progressive Exit Engine).

Exit model:
  TP1  @ 0.4R → partial close 50% + SL → BE
  L2   @ 0.8R → SL → +0.3R
  Trail@ 1.2R → trailing SL ON (distance = 0.6R from anchor)

Migration note:
  Run migrate_initial_sl() once before starting the bot.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, text

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


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _calc_r(entry: float, initial_sl: float, price: float, direction: str) -> float:
    """R-multiple from entry using the *original* SL distance."""
    risk_unit = abs(entry - initial_sl)
    if risk_unit < 1e-9:
        return 0.0
    if direction == TradeDirection.LONG.value:
        return (price - entry) / risk_unit
    return (entry - price) / risk_unit


async def migrate_initial_sl() -> None:
    """
    One-time migration:
    1. Add new columns if missing.
    2. Fill initial_sl for open trades that pre-date this version.
    """
    ddl_statements = [
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS initial_sl       FLOAT   DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS is_migrated      BOOLEAN DEFAULT FALSE",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp1_hit          BOOLEAN DEFAULT FALSE",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS layer2_locked    BOOLEAN DEFAULT FALSE",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS trailing_active  BOOLEAN DEFAULT FALSE",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS trailing_anchor  FLOAT   DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS sl_layer2        FLOAT   DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS realized_pnl     FLOAT   DEFAULT 0",
    ]
    async with AsyncSessionLocal() as s:
        for stmt in ddl_statements:
            await s.execute(text(stmt))

        # Fill initial_sl for existing open trades
        # If stop_loss == entry_price (already at BE), use 2% fallback
        await s.execute(text("""
            UPDATE trades
            SET initial_sl  = CASE
                                  WHEN ABS(entry_zone_low + entry_zone_high) / 2 - stop_loss < 0.0001
                                  THEN (entry_zone_low + entry_zone_high) / 2 * 0.98
                                  ELSE stop_loss
                              END,
                is_migrated = TRUE
            WHERE status IN ('PENDING', 'TRIGGERED')
              AND (initial_sl = 0 OR initial_sl IS NULL)
        """))
        await s.commit()
    logger.info("✅ migrate_initial_sl() complete")


# ─────────────────────────────────────────────
# PaperExecutor
# ─────────────────────────────────────────────

class PaperExecutor:

    def __init__(self) -> None:
        self.cfg             = settings.paper_executor
        self.initial_capital = self.cfg["initial_capital_usd"]
        self.risk_pct        = self.cfg["risk_per_trade_pct"]
        self.slippage_entry  = self.cfg["slippage_entry_pct"]
        self.slippage_stop   = self.cfg["slippage_stop_pct"]
        self.spread          = self.cfg["spread_pct"]
        self.fee_pct         = settings.backtest["fee_pct"]

    # ── Portfolio ────────────────────────────────────────────────────────

    async def get_equity(self) -> float:
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value])
                )
            )
            closed   = res.scalars().all()
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
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value])
                ).order_by(Trade.created_at.desc())
            )
            trades = res.scalars().all()
            for t in trades:
                t.entry_price = t.actual_entry_price or (
                    (t.entry_zone_low + t.entry_zone_high) / 2
                )
                t.tp1      = t.take_profit_1
                t.tp2      = t.take_profit_2
                t.opened_at = t.triggered_at or t.created_at
            return trades

    async def is_kill_switch_active(self) -> bool:
        async with AsyncSessionLocal() as s:
            today_start = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            res = await s.execute(
                select(Trade).where(
                    Trade.closed_at >= today_start,
                    Trade.status.in_(
                        [TradeStatus.CLOSED_TP.value, TradeStatus.CLOSED_SL.value]
                    ),
                )
            )
            today_closed = sorted(res.scalars().all(), key=lambda t: t.closed_at)

        equity    = await self.get_equity()
        daily_pnl = sum((t.pnl_usd or 0) for t in today_closed)
        if equity > 0 and daily_pnl < 0:
            if abs(daily_pnl) / self.initial_capital >= self.cfg["daily_max_loss_pct"]:
                logger.warning(f"KILL SWITCH: daily loss ${daily_pnl:.2f}")
                return True

        recent = sorted(today_closed, key=lambda t: t.closed_at, reverse=True)
        consec  = 0
        for t in recent:
            if (t.pnl_usd or 0) < 0:
                consec += 1
            else:
                break
        if consec >= self.cfg["daily_max_consecutive_losses"]:
            logger.warning(f"KILL SWITCH: {consec} consecutive losses")
            return True
        return False

    # ── Sizing ───────────────────────────────────────────────────────────

    def _compute_size(
        self, equity: float, entry: float, sl: float, size_factor: float
    ) -> tuple[float, float]:
        risk_amount  = equity * self.risk_pct * size_factor
        risk_per_unit = abs(entry - sl)
        if risk_per_unit == 0:
            return 0.0, 0.0
        units    = risk_amount / risk_per_unit
        notional = min(units * entry, equity * 0.20)
        return notional, risk_amount

    # ── Trade lifecycle ──────────────────────────────────────────────────

    async def submit(self, card: TradeCard) -> Optional[int]:
        if await self.is_kill_switch_active():
            logger.warning(f"Kill switch active, skipping {card.symbol}")
            return None

        equity = await self.get_equity()
        if await self.get_open_count() >= self.cfg["max_concurrent_trades"]:
            logger.info(f"Max concurrent trades reached, skipping {card.symbol}")
            return None

        entry_mid = (card.entry_zone_low + card.entry_zone_high) / 2
        notional, risk = self._compute_size(
            equity, entry_mid, card.stop_loss, card.size_factor
        )
        if notional <= 0:
            return None

        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.symbol == card.symbol,
                    Trade.status.in_(
                        [TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value]
                    ),
                )
            )
            if res.scalars().first():
                logger.info(f"{card.symbol}: trade already active, skipping")
                return None

            trade = Trade(
                symbol               = card.symbol,
                direction            = (
                    TradeDirection.LONG.value
                    if card.direction == "LONG"
                    else TradeDirection.SHORT.value
                ),
                status               = TradeStatus.PENDING.value,
                setup_score          = card.setup_score,
                market_state         = card.market_state.value,
                market_regime        = card.market_regime.value,
                trigger_description  = card.trigger_description,
                trigger_confirmed_count = 0,
                invalidation_condition  = card.invalidation_condition,
                entry_zone_low       = card.entry_zone_low,
                entry_zone_high      = card.entry_zone_high,
                stop_loss            = card.stop_loss,
                initial_sl           = card.stop_loss,   # ← snapshot — NEVER change
                take_profit_1        = card.tp1,
                take_profit_2        = card.tp2,
                take_profit_3        = card.tp3,
                risk_reward_ratio    = card.risk_reward,
                position_size_usd    = notional,
                risk_amount_usd      = risk,
                created_at           = card.created_at,
                notes                = " | ".join(card.reasoning[:5]),
                is_migrated          = False,
                tp1_hit              = False,
                layer2_locked        = False,
                trailing_active      = False,
                trailing_anchor      = 0.0,
                sl_layer2            = 0.0,
                realized_pnl         = 0.0,
            )
            s.add(trade)
            await s.commit()
            await s.refresh(trade)
            logger.info(
                f"📝 PENDING {card.direction} {card.symbol} | "
                f"size=${notional:.0f} | risk=${risk:.2f} | "
                f"entry=[{card.entry_zone_low:.4g},{card.entry_zone_high:.4g}] | "
                f"SL={card.stop_loss:.4g} | TP2={card.tp2:.4g} | RR={card.risk_reward:.2f}"
            )
            return trade.id

    async def trigger(
        self, trade_id: int, fill_price: float, confirmed_count: int
    ) -> None:
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, trade_id)
            if t is None or t.status != TradeStatus.PENDING.value:
                return
            slip   = 1 + self.slippage_entry + self.spread / 2
            actual = fill_price * slip if t.direction == TradeDirection.LONG.value else fill_price / slip
            t.actual_entry_price       = actual
            t.status                   = TradeStatus.TRIGGERED.value
            t.triggered_at             = datetime.utcnow()
            t.trigger_confirmed_count  = confirmed_count
            t.fees_usd                 = (t.position_size_usd or 0) * self.fee_pct
            # Guarantee initial_sl is set (safety net for migrated trades)
            if not t.initial_sl:
                t.initial_sl = t.stop_loss
            await s.commit()
            logger.info(f"🎯 TRIGGERED {t.symbol} @ {actual:.4g}")

    async def _partial_close(
        self, t: Trade, pct: float, price: float, reason: str
    ) -> None:
        """Close *pct* fraction of the position. Updates realized_pnl in place."""
        if pct <= 0 or pct >= 1:
            return
        entry        = t.actual_entry_price or 0
        closed_size  = (t.position_size_usd or 0) * pct
        units_closed = closed_size / entry if entry else 0

        if t.direction == TradeDirection.LONG.value:
            pnl = (price - entry) * units_closed
        else:
            pnl = (entry - price) * units_closed

        t.position_size_usd  = (t.position_size_usd or 0) * (1 - pct)
        t.realized_pnl       = (t.realized_pnl or 0) + pnl
        t.fees_usd           = (t.fees_usd or 0) + closed_size * self.fee_pct
        t.notes              = (
            f"{t.notes or ''} | PARTIAL {pct*100:.0f}% @{price:.4g} "
            f"pnl={pnl:+.2f} [{reason}]"
        )
        logger.info(
            f"✂️  PARTIAL {t.symbol} {pct*100:.0f}% @{price:.4g} | "
            f"pnl={pnl:+.2f} | remaining=${t.position_size_usd:.0f}"
        )

    async def close(
        self, trade_id: int, exit_price: float, status: TradeStatus, reason: str = ""
    ) -> None:
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, trade_id)
            if t is None or t.status != TradeStatus.TRIGGERED.value:
                return

            actual_exit = exit_price
            if status == TradeStatus.CLOSED_SL:
                slip        = 1 + self.slippage_stop + self.spread / 2
                actual_exit = (
                    exit_price / slip
                    if t.direction == TradeDirection.LONG.value
                    else exit_price * slip
                )

            entry = t.actual_entry_price or 0
            units = (t.position_size_usd or 0) / entry if entry else 0
            if t.direction == TradeDirection.LONG.value:
                pnl = (actual_exit - entry) * units
            else:
                pnl = (entry - actual_exit) * units

            # Add partial gains already realized
            total_pnl = pnl + (t.realized_pnl or 0)
            risk      = t.risk_amount_usd or 1
            pnl_r     = total_pnl / risk

            t.exit_price = actual_exit
            t.pnl_usd    = total_pnl
            t.pnl_r      = pnl_r
            t.fees_usd   = (t.fees_usd or 0) + (t.position_size_usd or 0) * self.fee_pct
            t.status     = status.value
            t.closed_at  = datetime.utcnow()
            if reason:
                t.notes = (t.notes or "") + f" | EXIT: {reason}"
            await s.commit()

            emoji = "✅" if total_pnl > 0 else "❌"
            logger.info(
                f"{emoji} CLOSED {t.symbol} {status.value} | "
                f"exit={actual_exit:.4g} | PnL=${total_pnl:+.2f} ({pnl_r:+.2f}R)"
            )
            await OutcomeLogger().log(t)

    async def cancel(self, trade_id: int, reason: str) -> None:
        async with AsyncSessionLocal() as s:
            t = await s.get(Trade, trade_id)
            if t is None or t.status != TradeStatus.PENDING.value:
                return
            t.status    = TradeStatus.CANCELLED.value
            t.closed_at = datetime.utcnow()
            t.notes     = (t.notes or "") + f" | CANCELLED: {reason}"
            await s.commit()
            logger.info(f"🚫 CANCELLED {t.symbol}: {reason}")

    # ── Tick handler ─────────────────────────────────────────────────────

    async def update_positions(self, prices: dict[str, float]) -> None:
        """Progressive exit engine — runs on every price tick."""
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                select(Trade).where(
                    Trade.status.in_(
                        [TradeStatus.PENDING.value, TradeStatus.TRIGGERED.value]
                    )
                )
            )
            active  = res.scalars().all()
            max_age = timedelta(hours=8)
            now     = datetime.utcnow()

            for t in active:
                price = prices.get(t.symbol)
                if price is None:
                    continue

                # ── PENDING ──────────────────────────────────────────────
                if t.status == TradeStatus.PENDING.value:
                    missed = self.cfg["missed_entry_max_pct"]
                    if t.direction == TradeDirection.LONG.value:
                        if price > t.entry_zone_high * (1 + missed):
                            await self.cancel(
                                t.id,
                                f"missed entry: {price:.4g} > zone {t.entry_zone_high:.4g}",
                            )
                            continue
                    else:
                        if price < t.entry_zone_low * (1 - missed):
                            await self.cancel(
                                t.id,
                                f"missed entry: {price:.4g} < zone {t.entry_zone_low:.4g}",
                            )
                            continue

                    if t.created_at and (now - t.created_at) > max_age:
                        await self.cancel(t.id, "pending expired (>8h)")
                        continue

                    if t.entry_zone_low <= price <= t.entry_zone_high:
                        await self.trigger(t.id, price, confirmed_count=1)
                        logger.info(f"⚡ AUTO-TRIGGERED {t.symbol} @ {price:.4g}")
                    continue

                # ── TRIGGERED — Progressive Exit Engine ─────────────────
                entry      = t.actual_entry_price or 0
                initial_sl = t.initial_sl or t.stop_loss   # fallback for migrated
                risk_unit  = abs(entry - initial_sl)

                # Guard: corrupted risk_unit → skip layers, use simple SL/TP
                if risk_unit < 1e-6:
                    if t.direction == TradeDirection.LONG.value:
                        if price <= t.stop_loss:
                            await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, "SL hit")
                        elif price >= t.take_profit_2:
                            await self.close(t.id, t.take_profit_2, TradeStatus.CLOSED_TP, "TP2 hit")
                    else:
                        if price >= t.stop_loss:
                            await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, "SL hit")
                        elif price <= t.take_profit_2:
                            await self.close(t.id, t.take_profit_2, TradeStatus.CLOSED_TP, "TP2 hit")
                    continue

                r = _calc_r(entry, initial_sl, price, t.direction)

                # ── Layer 1: TP1 @ 0.4R ──────────────────────────────────
                if not t.tp1_hit and r >= 0.4:
                    await self._partial_close(t, 0.5, price, "TP1")
                    t.stop_loss = entry              # SL → BE
                    t.tp1_hit   = True
                    logger.info(
                        f"🏁 [TP1] {t.symbol} r={r:.2f} | "
                        f"50% closed @{price:.4g} | SL→BE"
                    )

                # ── Layer 2: lock @ 0.8R ─────────────────────────────────
                if t.tp1_hit and not t.layer2_locked and r >= 0.8:
                    sl2 = (
                        entry + 0.3 * risk_unit
                        if t.direction == TradeDirection.LONG.value
                        else entry - 0.3 * risk_unit
                    )
                    t.stop_loss    = sl2
                    t.sl_layer2    = sl2
                    t.layer2_locked = True
                    logger.info(
                        f"🔒 [L2] {t.symbol} r={r:.2f} | "
                        f"SL→+0.3R ({sl2:.4g})"
                    )

                # ── Layer 3: Trailing ON @ 1.2R ───────────────────────────
                if t.layer2_locked and not t.trailing_active and r >= 1.2:
                    t.trailing_active = True
                    t.trailing_anchor = price
                    logger.info(
                        f"🚀 [TRAIL ON] {t.symbol} r={r:.2f} | "
                        f"anchor={price:.4g}"
                    )

                # ── Trailing SL update ────────────────────────────────────
                if t.trailing_active:
                    if t.direction == TradeDirection.LONG.value:
                        t.trailing_anchor = max(t.trailing_anchor, price)
                        new_sl = t.trailing_anchor - 0.6 * risk_unit
                        if new_sl > t.stop_loss:       # only move forward
                            t.stop_loss = new_sl
                            logger.info(
                                f"📈 [TRAIL MOVE] {t.symbol} "
                                f"anchor={t.trailing_anchor:.4g} sl={new_sl:.4g}"
                            )
                    else:
                        t.trailing_anchor = min(t.trailing_anchor, price)
                        new_sl = t.trailing_anchor + 0.6 * risk_unit
                        if new_sl < t.stop_loss:
                            t.stop_loss = new_sl
                            logger.info(
                                f"📉 [TRAIL MOVE] {t.symbol} "
                                f"anchor={t.trailing_anchor:.4g} sl={new_sl:.4g}"
                            )

                # ── Exit check (SL / Trailing SL) ────────────────────────
                if t.direction == TradeDirection.LONG.value:
                    if price <= t.stop_loss:
                        label = "Trailing SL" if t.trailing_active else (
                            "L2 SL" if t.layer2_locked else
                            "BE SL" if t.tp1_hit else "SL hit"
                        )
                        await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, label)
                else:
                    if price >= t.stop_loss:
                        label = "Trailing SL" if t.trailing_active else (
                            "L2 SL" if t.layer2_locked else
                            "BE SL" if t.tp1_hit else "SL hit"
                        )
                        await self.close(t.id, t.stop_loss, TradeStatus.CLOSED_SL, label)

            await s.commit()

    # ── Portfolio snapshot ───────────────────────────────────────────────

    async def take_portfolio_snapshot(self) -> None:
        async with AsyncSessionLocal() as s:
            equity     = await self.get_equity()
            open_count = await self.get_open_count()
            kill       = await self.is_kill_switch_active()
            snap = PortfolioSnapshot(
                equity_usd          = equity,
                open_positions      = open_count,
                daily_pnl_usd       = 0.0,
                daily_pnl_pct       = 0.0,
                consecutive_losses  = 0,
                kill_switch_active  = kill,
            )
            s.add(snap)
            await s.commit()
