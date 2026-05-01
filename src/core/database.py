"""SQLAlchemy ORM models + async session factory.

Designed for SQLite now, PostgreSQL-ready (no SQLite-specific types).
Switch by changing DATABASE_URL — that's the only change needed.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.core.config import settings


class Base(DeclarativeBase):
    pass


# ---------- Enums ----------

class TradeDirection(str, PyEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeStatus(str, PyEnum):
    PENDING = "PENDING"          # waiting for trigger
    TRIGGERED = "TRIGGERED"      # entry filled (paper)
    CLOSED_TP = "CLOSED_TP"
    CLOSED_SL = "CLOSED_SL"
    CANCELLED = "CANCELLED"      # invalidation hit / missed entry
    EXPIRED = "EXPIRED"


class MarketState(str, PyEnum):
    CROWDED_LONG_TRAP = "CROWDED_LONG_TRAP"
    SHORT_SQUEEZE_SETUP = "SHORT_SQUEEZE_SETUP"
    SMART_MONEY_DIVERGENCE = "SMART_MONEY_DIVERGENCE"
    EXHAUSTION = "EXHAUSTION"
    ACCUMULATION = "ACCUMULATION"
    NO_SETUP = "NO_SETUP"


class MarketRegime(str, PyEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"


# ---------- Tables ----------

class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    base_asset: Mapped[str] = mapped_column(String(16))
    quote_asset: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ScanSnapshot(Base):
    """Output of Layer 1 scanner — which coins passed filters at time T."""
    __tablename__ = "scan_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True, default=func.now())
    price: Mapped[float] = mapped_column(Float)
    volume_24h_usd: Mapped[float] = mapped_column(Float)
    open_interest_usd: Mapped[float] = mapped_column(Float)
    funding_rate: Mapped[float] = mapped_column(Float)
    long_short_ratio: Mapped[float] = mapped_column(Float)
    oi_change_4h_pct: Mapped[float] = mapped_column(Float, default=0.0)
    passed_filters: Mapped[bool] = mapped_column(Boolean, default=False)
    extremity_score: Mapped[float] = mapped_column(Float, default=0.0)


class MarketSnapshot(Base):
    """Detailed snapshot for shortlisted coins (Layer 2 output)."""
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True, default=func.now())
    price: Mapped[float] = mapped_column(Float)
    funding_rate: Mapped[float] = mapped_column(Float)
    open_interest: Mapped[float] = mapped_column(Float)
    open_interest_usd: Mapped[float] = mapped_column(Float)
    long_short_ratio_global: Mapped[float] = mapped_column(Float)
    long_short_ratio_top: Mapped[float] = mapped_column(Float)
    taker_buy_volume: Mapped[float] = mapped_column(Float)
    taker_sell_volume: Mapped[float] = mapped_column(Float)
    market_state: Mapped[Optional[str]] = mapped_column(Enum(MarketState), nullable=True)
    market_regime: Mapped[Optional[str]] = mapped_column(Enum(MarketRegime), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    raw_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_snapshot_symbol_time", "symbol", "timestamp"),
    )


class LiquidityZone(Base):
    """Identified liquidity pool above/below price."""
    __tablename__ = "liquidity_zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    price_level: Mapped[float] = mapped_column(Float)
    side: Mapped[str] = mapped_column(String(8))  # "ABOVE" / "BELOW"
    estimated_liquidations_usd: Mapped[float] = mapped_column(Float, default=0.0)
    distance_pct: Mapped[float] = mapped_column(Float)  # % from current price
    strength: Mapped[float] = mapped_column(Float, default=0.0)  # 0-1 confidence
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Trade(Base):
    """Paper trade record — full lifecycle."""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(Enum(TradeDirection))
    status: Mapped[str] = mapped_column(Enum(TradeStatus), default=TradeStatus.PENDING, index=True)

    # Setup details
    setup_score: Mapped[float] = mapped_column(Float)
    market_state: Mapped[str] = mapped_column(Enum(MarketState))
    market_regime: Mapped[str] = mapped_column(Enum(MarketRegime))
    trigger_description: Mapped[str] = mapped_column(String(512))
    trigger_confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    invalidation_condition: Mapped[str] = mapped_column(String(512))

    # Prices
    entry_zone_low: Mapped[float] = mapped_column(Float)
    entry_zone_high: Mapped[float] = mapped_column(Float)
    actual_entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit_1: Mapped[float] = mapped_column(Float)
    take_profit_2: Mapped[float] = mapped_column(Float)
    take_profit_3: Mapped[float] = mapped_column(Float)
    risk_reward_ratio: Mapped[float] = mapped_column(Float)

    # Sizing
    position_size_usd: Mapped[float] = mapped_column(Float)
    risk_amount_usd: Mapped[float] = mapped_column(Float)

    # Result (filled on close)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # in R units
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)




class TradeOutcome(Base):
    """Learning Loop — structured outcome per closed trade."""
    __tablename__ = "trade_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trades.id"), unique=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    setup_type: Mapped[str] = mapped_column(Enum(MarketState), index=True)
    regime: Mapped[str] = mapped_column(Enum(MarketRegime), index=True)
    direction: Mapped[str] = mapped_column(Enum(TradeDirection))
    setup_score: Mapped[float] = mapped_column(Float)
    funding_at_entry: Mapped[float] = mapped_column(Float, default=0.0)
    ls_global_at_entry: Mapped[float] = mapped_column(Float, default=1.0)
    oi_change_4h_at_entry: Mapped[float] = mapped_column(Float, default=0.0)
    result: Mapped[str] = mapped_column(String(12))
    pnl_r: Mapped[float] = mapped_column(Float)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    closed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)

    __table_args__ = (
        Index("ix_outcome_setup_regime", "setup_type", "regime"),
    )

class PortfolioSnapshot(Base):
    """Daily equity / drawdown tracking."""
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    equity_usd: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    daily_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=False)


class AlertLog(Base):
    """Sent telegram alerts."""
    __tablename__ = "alert_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    alert_type: Mapped[str] = mapped_column(String(32))  # SETUP / TRIGGER / EXIT
    symbol: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(2048))
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)


# ---------- Engine & Session ----------

engine = create_async_engine(
    settings.env.database_url,
    echo=False,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    """Create all tables. Idempotent."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    return AsyncSessionLocal()
