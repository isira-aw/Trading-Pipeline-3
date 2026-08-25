"""Position matching and realized P&L (§5.1, §5.2, §5.4).

Matching is **FIFO**: a sell closes the oldest open lot first. The
alternative, average cost, blends every entry into one price and loses the
per-entry attribution the model registry needs — with FIFO each closed
position keeps the `model_id` of the *buy* that opened it, so a model's win
rate reflects the entries it actually signalled. Exits are not attributed,
since a sell may come from stop logic rather than any model.

Everything here is a pure function over trade rows, so the arithmetic is
testable against hand-computed numbers without a database or an exchange.

**Fees are included.** At the default 1% target move, a 0.1% taker fee each
way consumes a fifth of the edge; a win rate computed on gross P&L would
overstate performance and could push a losing strategy through the §5.4
promotion gate.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

SIDE_BUY = "buy"
SIDE_SELL = "sell"


@dataclass
class TradeRecord:
    """The subset of a `trades` row that matching needs."""

    id: str
    symbol: str
    side: str
    quantity: float
    price: float
    created_at: datetime
    model_id: str | None = None
    fee_usdt: float = 0.0

    @property
    def fee_per_unit(self) -> float:
        return self.fee_usdt / self.quantity if self.quantity else 0.0


@dataclass
class OpenLot:
    """An entry that has not been fully closed yet."""

    trade_id: str
    symbol: str
    quantity: float
    remaining: float
    price: float
    opened_at: datetime
    model_id: str | None
    fee_per_unit: float

    @property
    def cost_basis(self) -> float:
        return self.remaining * self.price


@dataclass
class ClosedPosition:
    """A matched buy/sell pair (or fraction of one)."""

    symbol: str
    quantity: float
    entry_price: float
    exit_price: float
    entry_at: datetime
    exit_at: datetime
    entry_trade_id: str
    exit_trade_id: str
    model_id: str | None
    gross_pnl: float
    fees: float

    @property
    def realized_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def realized_pnl_pct(self) -> float:
        basis = self.entry_price * self.quantity
        return (self.realized_pnl / basis * 100.0) if basis else 0.0

    @property
    def is_win(self) -> bool:
        """Net of fees. A position that moved in the right direction but did
        not cover its costs is not a win."""
        return self.realized_pnl > 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "entry_at": self.entry_at.isoformat(),
            "exit_at": self.exit_at.isoformat(),
            "model_id": self.model_id,
            "gross_pnl": self.gross_pnl,
            "fees": self.fees,
            "realized_pnl": self.realized_pnl,
            "realized_pnl_pct": self.realized_pnl_pct,
            "is_win": self.is_win,
        }


@dataclass
class MatchResult:
    closed: list[ClosedPosition] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)
    # Sell quantity with no matching open lot. Should be zero in normal
    # operation; a non-zero value means trades are missing from the history
    # (e.g. a position opened before the recorded window) and is surfaced
    # rather than silently dropped.
    unmatched_sell_quantity: float = 0.0


def match_fifo(trades: list[TradeRecord]) -> MatchResult:
    """Match buys to sells oldest-first, per symbol.

    Input need not be sorted; it is sorted by `created_at` here so the
    result does not depend on query order.
    """
    result = MatchResult()
    lots_by_symbol: dict[str, list[OpenLot]] = {}

    for trade in sorted(trades, key=lambda t: (t.created_at, t.id)):
        lots = lots_by_symbol.setdefault(trade.symbol, [])

        if trade.side == SIDE_BUY:
            lots.append(
                OpenLot(
                    trade_id=trade.id,
                    symbol=trade.symbol,
                    quantity=trade.quantity,
                    remaining=trade.quantity,
                    price=trade.price,
                    opened_at=trade.created_at,
                    model_id=trade.model_id,
                    fee_per_unit=trade.fee_per_unit,
                )
            )
            continue

        if trade.side != SIDE_SELL:
            logger.warning("Ignoring trade %s with unknown side %r", trade.id, trade.side)
            continue

        to_close = trade.quantity
        exit_fee_per_unit = trade.fee_per_unit

        while to_close > 0 and lots:
            lot = lots[0]
            matched = min(to_close, lot.remaining)

            gross = (trade.price - lot.price) * matched
            fees = matched * (lot.fee_per_unit + exit_fee_per_unit)

            result.closed.append(
                ClosedPosition(
                    symbol=trade.symbol,
                    quantity=matched,
                    entry_price=lot.price,
                    exit_price=trade.price,
                    entry_at=lot.opened_at,
                    exit_at=trade.created_at,
                    entry_trade_id=lot.trade_id,
                    exit_trade_id=trade.id,
                    # Attributed to the model that opened the position.
                    model_id=lot.model_id,
                    gross_pnl=gross,
                    fees=fees,
                )
            )

            lot.remaining -= matched
            to_close -= matched
            if lot.remaining <= 1e-12:
                lots.pop(0)

        if to_close > 1e-12:
            result.unmatched_sell_quantity += to_close
            logger.warning(
                "Sell %s for %s exceeded open lots by %.8f — history may be incomplete.",
                trade.id, trade.symbol, to_close,
            )

    for lots in lots_by_symbol.values():
        result.open_lots.extend(lots)

    return result


def max_drawdown_pct(closed: list[ClosedPosition]) -> float:
    """Peak-to-trough drop of the cumulative realized P&L curve, as a
    percentage of the running peak (§5.4 promotion gate).

    Returns 0.0 when the curve never drops below a prior peak.
    """
    if not closed:
        return 0.0

    ordered = sorted(closed, key=lambda p: p.exit_at)

    cumulative = 0.0
    peak = 0.0
    worst = 0.0

    for position in ordered:
        cumulative += position.realized_pnl
        peak = max(peak, cumulative)
        if peak > 0:
            drawdown = (peak - cumulative) / peak * 100.0
            worst = max(worst, drawdown)

    return worst


def compute_stats(closed: list[ClosedPosition]) -> dict:
    """Aggregate realized performance (§5.2 dashboard, §5.4 gate)."""
    if not closed:
        return {
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "total_realized_pnl": 0.0,
            "total_fees": 0.0,
            "gross_pnl": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
        }

    wins = [p for p in closed if p.is_win]
    losses = [p for p in closed if not p.is_win]

    return {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(closed),
        "total_realized_pnl": sum(p.realized_pnl for p in closed),
        "total_fees": sum(p.fees for p in closed),
        "gross_pnl": sum(p.gross_pnl for p in closed),
        "max_drawdown_pct": max_drawdown_pct(closed),
        "avg_win": (sum(p.realized_pnl for p in wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(p.realized_pnl for p in losses) / len(losses)) if losses else 0.0,
    }


def first_entry_at(closed: list[ClosedPosition], open_lots: list[OpenLot]) -> datetime | None:
    """Earliest position entry, for the promotion gate's "days trading"."""
    candidates = [p.entry_at for p in closed] + [lot.opened_at for lot in open_lots]
    return min(candidates) if candidates else None
