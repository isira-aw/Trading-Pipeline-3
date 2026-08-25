from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, BigInteger, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import UUID, TSTZRANGE, JSONB
import uuid

Base = declarative_base()

class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        # Required by §3 and relied on by the data downloader's ON CONFLICT upsert.
        UniqueConstraint("symbol", "interval", "open_time", name="uq_candles_symbol_interval_open_time"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)
    interval = Column(String(10), nullable=False)
    open_time = Column(DateTime(timezone=True), nullable=False)
    open = Column(Numeric, nullable=False)
    high = Column(Numeric, nullable=False)
    low = Column(Numeric, nullable=False)
    close = Column(Numeric, nullable=False)
    volume = Column(Numeric, nullable=False)

class Model(Base):
    __tablename__ = "models"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol = Column(String(20), nullable=False)
    model_type = Column(String(50), nullable=False)
    file_path = Column(String, nullable=False)
    trained_at = Column(DateTime(timezone=True), nullable=False)
    training_data_range = Column(TSTZRANGE)
    metrics = Column(JSONB, nullable=False)
    status = Column(String(20), nullable=False, default='candidate')
    notes = Column(String)

class Trade(Base):
    __tablename__ = "trades"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stage = Column(String(10), nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    order_type = Column(String(10), nullable=False)
    quantity = Column(Numeric, nullable=False)
    price = Column(Numeric)
    model_id = Column(UUID(as_uuid=True), ForeignKey('models.id'))
    model_confidence = Column(Numeric)
    risk_decision = Column(String(10), nullable=False)
    risk_notes = Column(JSONB)
    llm_context = Column(JSONB)
    status = Column(String(15), nullable=False)
    binance_order_id = Column(String(50))
    # Total fees for this trade in USDT. Realized P&L is computed net of
    # fees, which at a 1% target move consume a meaningful part of the edge.
    fee_usdt = Column(Numeric, nullable=False, server_default="0")
    # Set on the ENTRY (buy) trade: the ATR-derived stop, fixed at open.
    stop_price = Column(Numeric)
    # Set on the EXIT (sell) trade: stop_hit | target_reached | horizon_elapsed.
    exit_reason = Column(String(20))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

class WalletSnapshot(Base):
    __tablename__ = "wallet_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    stage = Column(String(10), nullable=False)
    balances = Column(JSONB, nullable=False)
    total_value_usdt = Column(Numeric, nullable=False)
    snapshot_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

class RiskLog(Base):
    __tablename__ = "risk_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trade_id = Column(UUID(as_uuid=True), ForeignKey('trades.id'))
    checks = Column(JSONB, nullable=False)
    decision = Column(String(10), nullable=False)
    reason = Column(String)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

class LLMAdvisory(Base):
    __tablename__ = "llm_advisories"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    provider = Column(String(20), nullable=False)
    prompt = Column(String, nullable=False)
    response = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

class Config(Base):
    __tablename__ = "config"

    key = Column(String(100), primary_key=True)
    value = Column(JSONB, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

class ComponentStatus(Base):
    __tablename__ = "component_status"

    component = Column(String(50), primary_key=True)
    status = Column(String(10), nullable=False)
    last_heartbeat = Column(DateTime(timezone=True), nullable=False)
    detail = Column(String)
