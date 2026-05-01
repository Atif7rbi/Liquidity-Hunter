"""High-level data fetching helpers — combines multiple endpoints into normalized objects."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

from src.core.logger import logger
from src.exchange.binance_client import BinanceFuturesClient


@dataclass
class TickerSummary:
    symbol: str
    price: float
    volume_24h_usd: float
    price_change_pct_24h: float


@dataclass
class FullSnapshot:
    symbol: str
    timestamp: datetime
    price: float
    funding_rate: float
    open_interest: float
    open_interest_usd: float
    ls_ratio_global: float
    ls_ratio_top: float
    taker_buy_volume: float
    taker_sell_volume: float
    oi_change_4h_pct: float
    klines_1h: pd.DataFrame
    klines_4h: pd.DataFrame
    klines_15m: pd.DataFrame


def klines_to_df(klines: list) -> pd.DataFrame:
    """Convert Binance klines list to pandas DataFrame."""
    if not klines:
        return pd.DataFrame()
    df = pd.DataFrame(
        klines,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ],
    )
    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[col] = pd.to_numeric(df[col])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    return df


async def fetch_all_tickers(client: BinanceFuturesClient) -> list[TickerSummary]:
    """One call → 24h stats for all perpetual symbols."""
    raw = await client.ticker_24h()
    perpetuals = set(await client.all_perpetual_symbols())
    out: list[TickerSummary] = []
    for t in raw:
        sym = t["symbol"]
        if sym not in perpetuals:
            continue
        try:
            out.append(
                TickerSummary(
                    symbol=sym,
                    price=float(t["lastPrice"]),
                    volume_24h_usd=float(t["quoteVolume"]),
                    price_change_pct_24h=float(t["priceChangePercent"]),
                )
            )
        except (KeyError, ValueError):
            continue
    return out


async def fetch_premium_index_all(client: BinanceFuturesClient) -> dict[str, dict]:
    """Mark price + funding for all symbols, keyed by symbol."""
    raw = await client.premium_index()
    return {item["symbol"]: item for item in raw}


async def fetch_full_snapshot(
    client: BinanceFuturesClient, symbol: str
) -> Optional[FullSnapshot]:
    """Pull everything we need for one symbol — runs requests in parallel."""
    try:
        results = await asyncio.gather(
            client.premium_index(symbol),
            client.open_interest(symbol),
            client.open_interest_hist(symbol, period="1h", limit=5),
            client.long_short_ratio(symbol, period="5m", limit=1, scope="global"),
            client.long_short_ratio(symbol, period="5m", limit=1, scope="top"),
            client.taker_buy_sell_volume(symbol, period="5m", limit=1),
            client.klines(symbol, "15m", 200),
            client.klines(symbol, "1h", 200),
            client.klines(symbol, "4h", 200),
            return_exceptions=True,
        )
        # Check for exceptions
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"Snapshot fetch partial fail for {symbol}: {r}")
                return None

        (premium, oi, oi_hist, ls_global, ls_top, taker, k15, k1h, k4h) = results

        price = float(premium["markPrice"])
        funding = float(premium["lastFundingRate"])
        oi_amount = float(oi["openInterest"])
        oi_usd = oi_amount * price

        # OI change vs 4h ago
        oi_change_4h = 0.0
        if oi_hist and len(oi_hist) >= 5:
            oi_now = float(oi_hist[-1]["sumOpenInterest"])
            oi_4h_ago = float(oi_hist[0]["sumOpenInterest"])
            if oi_4h_ago > 0:
                oi_change_4h = (oi_now - oi_4h_ago) / oi_4h_ago

        ls_g = float(ls_global[0]["longShortRatio"]) if ls_global else 1.0
        ls_t = float(ls_top[0]["longShortRatio"]) if ls_top else 1.0
        taker_buy = float(taker[0]["buySellRatio"]) if taker else 1.0

        return FullSnapshot(
            symbol=symbol,
            timestamp=datetime.utcnow(),
            price=price,
            funding_rate=funding,
            open_interest=oi_amount,
            open_interest_usd=oi_usd,
            ls_ratio_global=ls_g,
            ls_ratio_top=ls_t,
            taker_buy_volume=taker_buy,
            taker_sell_volume=1.0 / taker_buy if taker_buy else 1.0,
            oi_change_4h_pct=oi_change_4h,
            klines_15m=klines_to_df(k15),
            klines_1h=klines_to_df(k1h),
            klines_4h=klines_to_df(k4h),
        )
    except Exception as e:
        logger.error(f"Full snapshot failed for {symbol}: {e}")
        return None
