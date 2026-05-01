"""Telegram alerter — uses your own bot.

Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env. We keep alerts terse and
information-dense — emojis only as visual anchors, not decoration.
"""
from __future__ import annotations

import html
from typing import Optional

import httpx

from src.core.config import settings
from src.core.database import AsyncSessionLocal, AlertLog
from src.core.logger import logger
from src.layers.trade_generator import TradeCard


class TelegramAlerter:
    def __init__(self) -> None:
        self.token = settings.env.telegram_bot_token
        self.chat_id = settings.env.telegram_chat_id
        self.enabled = settings.alerts["telegram_enabled"] and bool(self.token) and bool(self.chat_id)
        if not self.enabled:
            logger.warning("Telegram disabled (token or chat_id missing)")

    async def _send(self, text: str, alert_type: str, symbol: str) -> None:
        if not self.enabled:
            await self._log(alert_type, symbol, text, sent=False, error="disabled")
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                resp.raise_for_status()
            await self._log(alert_type, symbol, text, sent=True)
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            await self._log(alert_type, symbol, text, sent=False, error=str(e))

    async def _log(self, alert_type: str, symbol: str, message: str, sent: bool, error: Optional[str] = None) -> None:
        async with AsyncSessionLocal() as s:
            s.add(AlertLog(alert_type=alert_type, symbol=symbol, message=message[:2000], sent=sent, error=error))
            await s.commit()

    # ---------- Templates ----------

    async def setup_alert(self, card: TradeCard) -> None:
        if not settings.alerts["alert_on_setup"]:
            return
        emoji = "🔻" if card.direction == "SHORT" else "🟢"
        size_label = "FULL" if card.size_factor >= 1.0 else "HALF"
        text = (
            f"{emoji} <b>SETUP — {card.symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Direction: <b>{card.direction}</b> ({size_label} size)\n"
            f"Score: <b>{card.setup_score:.1f}/100</b>\n"
            f"State: {card.market_state.value}\n"
            f"Regime: {card.market_regime.value}\n\n"
            f"<b>Trigger</b>\n{html.escape(card.trigger_description)}\n\n"
            f"<b>Levels</b>\n"
            f"Entry: {card.entry_zone_low:.4g} – {card.entry_zone_high:.4g}\n"
            f"SL: {card.stop_loss:.4g}\n"
            f"TP1: {card.tp1:.4g}\n"
            f"TP2: {card.tp2:.4g}\n"
            f"TP3: {card.tp3:.4g}\n"
            f"R:R: <b>{card.risk_reward:.2f}</b>\n\n"
            f"<b>Invalidation</b>\n{html.escape(card.invalidation_condition)}\n\n"
            f"Est. success: {card.estimated_success*100:.0f}%"
        )
        await self._send(text, "SETUP", card.symbol)

    async def trigger_alert(self, card: TradeCard, fill_price: float) -> None:
        if not settings.alerts["alert_on_trigger"]:
            return
        text = (
            f"🎯 <b>TRIGGERED — {card.symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{card.direction} filled at <b>{fill_price:.4g}</b>\n"
            f"SL: {card.stop_loss:.4g} | TP2: {card.tp2:.4g}"
        )
        await self._send(text, "TRIGGER", card.symbol)

    async def exit_alert(self, symbol: str, status: str, exit_price: float, pnl_r: float, pnl_usd: float) -> None:
        if not settings.alerts["alert_on_exit"]:
            return
        emoji = "✅" if pnl_usd > 0 else "❌"
        text = (
            f"{emoji} <b>CLOSED — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Status: {status}\n"
            f"Exit: {exit_price:.4g}\n"
            f"PnL: <b>{pnl_usd:+.2f} USD</b> ({pnl_r:+.2f}R)"
        )
        await self._send(text, "EXIT", symbol)

    async def info(self, message: str) -> None:
        await self._send(f"ℹ️ {html.escape(message)}", "INFO", "system")
