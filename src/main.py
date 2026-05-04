"""Main Orchestrator — wires all layers together and runs the bot loop.

Two modes:
  - `bot`        : run the live (paper) trading loop
  - `backtest`   : run backtest on a symbol
  - `scan`       : single scan cycle, print results

Usage:
  python -m src.main bot
  python -m src.main backtest --symbol BTCUSDT --days 60
  python -m src.main scan
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from typing import Optional

from src.alerts.telegram_bot import TelegramAlerter
from src.backtest.engine import BacktestEngine
from src.core.config import settings
from src.core.database import MarketRegime, MarketState, init_db
from src.core.logger import logger
from src.layers.context import ContextLayer
from src.layers.data_collector import DataCollector
from src.layers.decision_engine import DecisionEngine
from src.layers.liquidity_engine import LiquidityEngine
from src.layers.paper_executor import PaperExecutor
from src.layers.positioning import PositioningAnalyzer
from src.layers.regime_detector import RegimeDetector
from src.layers.scanner import Scanner
from src.layers.trade_generator import TradeGenerator
from src.layers.trigger_confirm import TriggerConfirmation
from src.learning.performance_analyzer import PerformanceAnalyzer
from src.learning.adaptive_weights import AdaptiveWeightsEngine


class LiquidityHunterBot:
    def __init__(self) -> None:
        self.scanner = Scanner()
        self.data_collector = DataCollector()
        self.liquidity_engine = LiquidityEngine()
        self.positioning = PositioningAnalyzer()
        self.context = ContextLayer()
        self.regime = RegimeDetector()
        self.decision = DecisionEngine()
        self.trigger = TriggerConfirmation()
        self.trade_gen = TradeGenerator()
        self.executor = PaperExecutor()
        self.alerter = TelegramAlerter()
        self.last_decisions: dict[str, dict] = {}  # for UI
        self.last_scan_results: list[dict] = []
        self.last_cycle_at: Optional[datetime] = None
        self.last_scan_diagnostics: dict = {}  # funnel stats for UI
        self._closed_since_adapt: int = 0

    async def run_cycle(self) -> dict:
        """One full pipeline cycle: scan → analyze → decide → submit."""
        cycle_start = datetime.utcnow()
        logger.info("=" * 60)
        logger.info(f"Cycle start: {cycle_start.isoformat()}")

        # Layer 1: scan all futures
        scan_results = await self.scanner.scan()
        # Capture diagnostics regardless of result
        if hasattr(self.scanner, "last_diagnostics") and self.scanner.last_diagnostics:
            d = self.scanner.last_diagnostics
            self.last_scan_diagnostics = {
                "total_symbols": d.total_symbols,
                "excluded_stablecoins": d.excluded_stablecoins,
                "passed_volume": d.passed_volume,
                "passed_oi": d.passed_oi,
                "passed_extremity": d.passed_extremity,
                "final_shortlist": d.final_shortlist,
            }
        if not scan_results:
            logger.warning("Scanner returned no shortlist")
            self.last_cycle_at = datetime.utcnow()
            return {"status": "no_setups", "shortlist": []}

        self.last_scan_results = [
            {
                "symbol": r.symbol,
                "price": r.price,
                "volume_24h_usd": r.volume_24h_usd,
                "open_interest_usd": r.open_interest_usd,
                "funding_rate": r.funding_rate,
                "long_short_ratio": r.long_short_ratio,
                "oi_change_4h_pct": r.oi_change_4h_pct,
                "extremity_score": r.extremity_score,
                "reasons": r.reasons,
            }
            for r in scan_results
        ]

        # Layer 2: detailed data
        symbols = [r.symbol for r in scan_results]
        snapshots = await self.data_collector.collect(symbols)

        # Process each symbol through layers 3-9
        decisions = []
        for sym, snap in snapshots.items():
            try:
                # Layer 3: Liquidity map
                lmap = self.liquidity_engine.build_map(snap)
                await self.liquidity_engine.persist(lmap)

                # Layer 4: Positioning
                # Need oi_change_4h from scanner result
                oi_change = next(
                    (r.oi_change_4h_pct for r in scan_results if r.symbol == sym), 0.0
                )
                pos = self.positioning.analyze(snap, oi_change)

                # Layer 5: Context
                ctx = self.context.analyze(snap, oi_change)

                # Layer 6: Regime
                regime = self.regime.detect(snap)

                # Layer 7: Decision
                dec = self.decision.evaluate(snap, lmap, pos, ctx, regime, oi_change)

                # Cache for UI
                self.last_decisions[sym] = {
                    "symbol": sym,
                    "price": snap.price,
                    "score": dec.score,
                    "direction": dec.direction,
                    "size_factor": dec.size_factor,
                    "components": dec.components,
                    "reasoning": dec.reasoning,
                    "state": pos.state.value,
                    "regime": regime.regime.value,
                    "imbalance": lmap.imbalance,
                    "dominant_side": lmap.dominant_side,
                    "primary_target": (
                        lmap.primary_target.price_level if lmap.primary_target else None
                    ),
                    "funding_rate": snap.funding_rate,
                    "ls_ratio": snap.ls_ratio_global,
                    "oi_usd": snap.open_interest_usd,
                    "checked_at": datetime.utcnow().isoformat(),
                }

                # Skip if WAIT
                if dec.direction == "WAIT":
                    continue

                # Layer 8: Trigger confirmation
                trig = self.trigger.check(snap, dec.direction, oi_change_5m=0.0)
                if not trig.confirmed:
                    logger.info(
                        f"{sym}: setup OK (score={dec.score:.1f}) but trigger not confirmed "
                        f"({trig.summary})"
                    )
                    continue

                # Layer 9: Build trade card
                card = self.trade_gen.generate(
                    snap, lmap, dec, pos.state, regime.regime
                )
                if card is None:
                    continue

                # Layer 10: Submit
                trade_id = await self.executor.submit(card)
                if trade_id:
                    decisions.append({"symbol": sym, "trade_id": trade_id, "card": card})
                    await self.alerter.setup_alert(card)
            except Exception as e:
                logger.exception(f"Pipeline error for {sym}: {e}")

        # Update positions with current prices
        prices = {sym: snap.price for sym, snap in snapshots.items()}
        await self.executor.update_positions(prices)
        await self.executor.take_portfolio_snapshot()

        self.last_cycle_at = datetime.utcnow()
        elapsed = (self.last_cycle_at - cycle_start).total_seconds()
        logger.info(f"Cycle done in {elapsed:.1f}s — {len(decisions)} new setups")

        if settings.section("learning").get("enabled", False):
            from sqlalchemy import select, func as sqlfunc
            from src.core.database import TradeOutcome, AsyncSessionLocal
            async with AsyncSessionLocal() as _s:
                _res = await _s.execute(select(sqlfunc.count()).select_from(TradeOutcome))
                _total = _res.scalar() or 0
            adapt_every = settings.section("learning").get("adapt_every_n_trades", 10)
            if _total > 0 and _total % adapt_every == 0:
                logger.info(f"🧠 Learning Loop: running adapt() at {_total} outcomes")
                _matrix = await PerformanceAnalyzer().analyze()
                AdaptiveWeightsEngine().adapt(_matrix)

        return {
            "status": "ok",
            "shortlist_count": len(scan_results),
            "snapshots_count": len(snapshots),
            "new_setups": len(decisions),
        }

    async def run_forever(self) -> None:
        """Background loop. Scan interval from config."""
        interval = settings.scanner["scan_interval_seconds"]
        logger.info(f"Bot loop started — interval {interval}s")
        await self.alerter.info("Liquidity Hunter Bot started.")
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.exception(f"Cycle exception: {e}")
            await asyncio.sleep(interval)


# ---------- CLI ----------

async def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["bot", "backtest", "scan", "init"])
    parser.add_argument("--symbol", type=str, default="BTCUSDT")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--timeframe", type=str, default="1h")
    args = parser.parse_args()

    await init_db()

    if args.mode == "init":
        logger.info("Database initialized.")
        return

    if args.mode == "scan":
        bot = LiquidityHunterBot()
        result = await bot.run_cycle()
        logger.info(f"Scan result: {result}")
        return

    if args.mode == "backtest":
        engine = BacktestEngine()
        stats = await engine.run(args.symbol, days=args.days, timeframe=args.timeframe)
        logger.info(f"Stats: {stats}")
        return

    if args.mode == "bot":
        bot = LiquidityHunterBot()
        await bot.run_forever()


if __name__ == "__main__":
    asyncio.run(main_cli())
