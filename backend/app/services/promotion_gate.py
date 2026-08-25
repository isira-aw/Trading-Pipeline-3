"""Promotion gate: paper -> live (§5.4).

Computes the four criteria from real paper-trading history — matched
positions from `trades` and the record in `wallet_snapshots`, all at
stage='paper' — and reports each one pass/fail with its current value, so
the dashboard can show a checklist rather than a bare yes/no.

This is the last thing between a simulation and real money, so two
properties matter more than convenience:

* **Every criterion is evaluated, always.** No short-circuit, so the
  checklist shows the full picture of what is and is not yet met.
* **Absent data fails.** A symbol with no closed trades has no win rate;
  treating that as "no failures recorded, therefore pass" would let an
  untested system reach live (§1.7).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WalletSnapshot
from app.services.config_service import get_config
from app.services.position_tracker import compute_stats, first_entry_at, match_fifo

logger = logging.getLogger(__name__)

STAGE_PAPER = "paper"
STAGE_LIVE = "live"
STAGE_SETUP = "setup"
STAGE_HALTED = "halted"

VALID_STAGES = (STAGE_SETUP, STAGE_PAPER, STAGE_LIVE, STAGE_HALTED)

# Bounds for the gate's own thresholds. A gate that demanded nothing would
# be indistinguishable from no gate, so the floors are non-negotiable.
GATE_BOUNDS: dict[str, tuple[float, float]] = {
    "min_paper_trading_days": (1, 365),
    "min_trade_count": (1, 100_000),
    "min_win_rate": (0.0, 1.0),
    "max_drawdown_pct": (0.0, 100.0),
}


@dataclass
class Criterion:
    name: str
    label: str
    passed: bool
    current: float | None
    required: float
    comparison: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "passed": self.passed,
            "current": self.current,
            "required": self.required,
            "comparison": self.comparison,
            "detail": self.detail,
        }


@dataclass
class GateResult:
    passed: bool
    criteria: list[Criterion] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)

    def failures(self) -> list[Criterion]:
        return [c for c in self.criteria if not c.passed]

    def summary(self) -> str:
        if self.passed:
            return "All promotion gate criteria met."
        return "; ".join(f"{c.label}: {c.detail}" for c in self.failures())

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "criteria": [c.to_dict() for c in self.criteria],
            "stats": self.stats,
            "thresholds": self.thresholds,
            "summary": self.summary(),
        }


async def paper_trading_days(db: AsyncSession, now: datetime) -> float | None:
    """Days of paper trading, measured from the earliest evidence of it.

    Uses the first position entry when one exists, falling back to the
    first wallet snapshot — a system that has been running and recording
    but has not yet traded still has elapsed days, and pretending otherwise
    would understate the record in the wrong direction only.
    """
    from app.services.trading_engine import load_trade_records

    records = await load_trade_records(db, STAGE_PAPER)
    matched = match_fifo(records)
    earliest = first_entry_at(matched.closed, matched.open_lots)

    if earliest is None:
        earliest = (
            await db.execute(
                select(WalletSnapshot.snapshot_at)
                .where(WalletSnapshot.stage == STAGE_PAPER)
                .order_by(WalletSnapshot.snapshot_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if earliest is None:
        return None

    return (now - earliest).total_seconds() / 86400.0


async def equity_drawdown_pct(
    db: AsyncSession, stage: str = STAGE_PAPER
) -> float | None:
    """Worst peak-to-trough decline of account equity, as a percentage.

    Measured from `wallet_snapshots`, NOT from the cumulative realized-P&L
    curve. Percent-of-peak on a P&L curve starting near zero is dominated
    by its own first few points — a +5 followed by a -5 has a peak of 5 and
    a trough of 0, i.e. a "100% drawdown" from a trivial swing. Against a
    15% gate limit that would make the criterion essentially unpassable for
    reasons unrelated to actual risk.

    Returns None when there is too little equity history to judge, which
    the caller treats as a failure rather than a pass (§1.7).
    """
    rows = (
        await db.execute(
            select(WalletSnapshot.total_value_usdt)
            .where(WalletSnapshot.stage == stage)
            .order_by(WalletSnapshot.snapshot_at.asc())
        )
    ).scalars().all()

    if len(rows) < 2:
        return None

    peak = float(rows[0])
    worst = 0.0
    for value in rows:
        equity = float(value)
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100.0)

    return worst


async def evaluate_gate(
    db: AsyncSession, now: datetime | None = None
) -> GateResult:
    """Evaluate all four §5.4 criteria against real paper-trading data."""
    now = now or datetime.now(timezone.utc)

    from app.services.trading_engine import load_trade_records

    thresholds = dict(await get_config(db, "promotion_gate"))

    records = await load_trade_records(db, STAGE_PAPER)
    matched = match_fifo(records)
    stats = compute_stats(matched.closed)

    days = await paper_trading_days(db, now)
    criteria: list[Criterion] = []

    # 1. Days of paper trading.
    required_days = float(thresholds.get("min_paper_trading_days", 0))
    criteria.append(Criterion(
        name="min_paper_trading_days",
        label="Paper trading duration",
        passed=days is not None and days >= required_days,
        current=round(days, 2) if days is not None else None,
        required=required_days,
        comparison=">=",
        detail=(
            "No paper trading history recorded yet."
            if days is None
            else f"{days:.1f} of {required_days:g} days required."
        ),
    ))

    # 2. Closed trade count. Counted over *closed* positions, matching the
    # basis the win rate is computed on — an open position has no outcome.
    closed = int(stats.get("closed_trades", 0))
    required_trades = float(thresholds.get("min_trade_count", 0))
    criteria.append(Criterion(
        name="min_trade_count",
        label="Closed trade count",
        passed=closed >= required_trades,
        current=closed,
        required=required_trades,
        comparison=">=",
        detail=f"{closed} of {required_trades:g} closed positions required.",
    ))

    # 3. Win rate, net of fees. None means nothing has closed, which fails:
    # absent evidence is not evidence of success (§1.7).
    win_rate = stats.get("win_rate")
    required_win_rate = float(thresholds.get("min_win_rate", 0))
    criteria.append(Criterion(
        name="min_win_rate",
        label="Win rate",
        passed=win_rate is not None and win_rate >= required_win_rate,
        current=round(win_rate, 4) if win_rate is not None else None,
        required=required_win_rate,
        comparison=">=",
        detail=(
            "No closed positions, so there is no win rate to judge."
            if win_rate is None
            else f"{win_rate:.1%} against {required_win_rate:.1%} required "
                 f"(net of fees)."
        ),
    ))

    # 4. Max drawdown of account equity, which must be BELOW the threshold.
    drawdown = await equity_drawdown_pct(db, STAGE_PAPER)
    max_drawdown = float(thresholds.get("max_drawdown_pct", 100))
    criteria.append(Criterion(
        name="max_drawdown_pct",
        label="Max drawdown",
        # Absent equity history fails. With nothing recorded the drawdown
        # would be trivially 0%, sailing past the limit and certifying an
        # untested record.
        passed=drawdown is not None and closed > 0 and drawdown <= max_drawdown,
        current=round(drawdown, 2) if drawdown is not None else None,
        required=max_drawdown,
        comparison="<=",
        detail=(
            "Not enough wallet history to measure equity drawdown."
            if drawdown is None
            else "No closed positions, so drawdown is untested."
            if closed == 0
            else f"{drawdown:.2f}% against a {max_drawdown:g}% limit."
        ),
    ))

    result = GateResult(
        passed=all(c.passed for c in criteria),
        criteria=criteria,
        stats={
            "closed_trades": closed,
            "wins": stats.get("wins", 0),
            "losses": stats.get("losses", 0),
            "win_rate": win_rate,
            "total_realized_pnl": stats.get("total_realized_pnl", 0.0),
            "total_fees": stats.get("total_fees", 0.0),
            "max_drawdown_pct": drawdown,
            "realized_pnl_curve_drawdown_pct": stats.get("max_drawdown_pct", 0.0),
            "paper_trading_days": round(days, 2) if days is not None else None,
            "open_positions": len(matched.open_lots),
        },
        thresholds=thresholds,
    )

    logger.info(
        "Promotion gate evaluated: passed=%s (%s)", result.passed, result.summary()
    )
    return result


def validate_thresholds(thresholds: dict) -> list[str]:
    """Check proposed gate thresholds. Returns a list of problems."""
    problems = []

    for key in GATE_BOUNDS:
        if key not in thresholds:
            problems.append(f"Missing required threshold: {key}")

    for key, value in thresholds.items():
        if key not in GATE_BOUNDS:
            problems.append(f"Unknown threshold: {key}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{key} must be a number.")
            continue
        low, high = GATE_BOUNDS[key]
        if not (low <= value <= high):
            problems.append(f"{key} must be between {low} and {high} (got {value}).")

    return problems
