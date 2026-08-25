"""Risk engine (§6, build order step 6).

Runs before every order attempt in both paper and live stages. It can veto
or resize any trade regardless of what the model or the LLM said (§1.5).

Structure: every rule is a pure function of (proposal, context, limits) with
no database, network or clock access — `now` is injected. All I/O lives in
`build_context`, so the rules are unit-testable in isolation.

No rule short-circuits. All of them run on every attempt and every result is
recorded, so `risk_log` shows the complete picture of why a trade was
stopped rather than only the first objection.

**Directionality.** Position sizing, exposure, daily loss and frequency
constrain *risk-increasing* orders only, i.e. buys. Applying them to sells
would resize or block an exit — leaving the system unable to close a large
or losing position, which is the opposite of risk management. Volatility
sanity and component health apply to every order, because a data glitch or
dead feed makes any order unsafe.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Candle, ComponentStatus, RiskLog, Trade, WalletSnapshot
from app.services.config_service import get_config

logger = logging.getLogger(__name__)

# Check names — stable identifiers, used as keys in risk_log.checks.
CHECK_POSITION_SIZE = "position_size"
CHECK_DAILY_LOSS = "daily_loss"
CHECK_CONFIDENCE = "model_confidence"
CHECK_VOLATILITY = "volatility_liquidity"
CHECK_EXPOSURE = "concentration"
CHECK_HEALTH = "component_health"
CHECK_FREQUENCY = "trade_frequency"

ACTION_PASS = "pass"
ACTION_RESIZE = "resize"
ACTION_REJECT = "reject"

DECISION_APPROVED = "approved"
DECISION_RESIZED = "resized"
DECISION_REJECTED = "rejected"

SIDE_BUY = "buy"
SIDE_SELL = "sell"

# Trade statuses that consume the daily frequency cap. A rejected proposal
# never becomes a trade row at all, and an order that failed or was
# cancelled took no position, so neither consumes the cap.
CAP_CONSUMING_STATUSES = ("filled", "partial")


@dataclass
class TradeProposal:
    """A trade the system wants to place, before risk approval."""

    symbol: str
    side: str
    quantity: float
    price: float
    confidence: float | None = None
    model_id: str | None = None

    @property
    def notional(self) -> float:
        return self.quantity * self.price

    @property
    def increases_risk(self) -> bool:
        return self.side == SIDE_BUY

    def snapshot(self) -> dict:
        """Embedded into risk_log.checks so a rejected attempt — which never
        becomes a `trades` row — still records what was proposed."""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "notional_usdt": self.notional,
            "confidence": self.confidence,
            "model_id": self.model_id,
        }


@dataclass
class DailyPnlBaseline:
    """Today's P&L against the day-open wallet value.

    `available=False` means no trustworthy baseline exists. That is not the
    same as 0% — §1.7 requires absent data to stop trading, not to pass.
    """

    available: bool
    pnl_pct: float | None = None
    baseline_at: datetime | None = None
    baseline_value: float | None = None
    current_value: float | None = None
    reason: str | None = None


@dataclass
class VolatilityStats:
    """Z-scores of the latest candle against its recent history."""

    available: bool
    range_z: float | None = None
    volume_z: float | None = None
    sample_size: int = 0
    reason: str | None = None


@dataclass
class ComponentHeartbeat:
    status: str
    last_heartbeat: datetime | None


@dataclass
class RiskContext:
    """Everything the rules need, gathered by `build_context`."""

    now: datetime
    wallet_total_usdt: float
    current_exposure_usdt: float
    daily_pnl: DailyPnlBaseline
    volatility: VolatilityStats
    components: dict[str, ComponentHeartbeat] = field(default_factory=dict)
    trades_today: int = 0


@dataclass
class CheckResult:
    name: str
    passed: bool
    action: str
    detail: str
    suggested_quantity: float | None = None

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "action": self.action,
            "detail": self.detail,
            "suggested_quantity": self.suggested_quantity,
        }


@dataclass
class RiskDecision:
    decision: str
    reason: str
    checks: list[CheckResult]
    final_quantity: float
    original_quantity: float

    @property
    def approved(self) -> bool:
        return self.decision in (DECISION_APPROVED, DECISION_RESIZED)

    def checks_dict(self) -> dict:
        return {check.name: check.to_dict() for check in self.checks}


# --------------------------------------------------------------------------
# Pure rules (§6). Each returns a CheckResult; none touches I/O.
# --------------------------------------------------------------------------


def check_position_size(
    proposal: TradeProposal, ctx: RiskContext, limits: dict
) -> CheckResult:
    """Single trade must be <= max_position_pct of total wallet value.

    Resizes down to exactly the limit; rejects if the resized order would be
    below the minimum viable notional (§6).
    """
    if not proposal.increases_risk:
        return CheckResult(
            CHECK_POSITION_SIZE, True, ACTION_PASS,
            "Sell order does not increase position size.",
        )

    max_pct = limits["max_position_pct"]
    min_notional = limits["min_order_notional_usdt"]

    if ctx.wallet_total_usdt <= 0:
        return CheckResult(
            CHECK_POSITION_SIZE, False, ACTION_REJECT,
            f"Wallet total is {ctx.wallet_total_usdt:.2f} USDT; cannot size a position.",
        )

    max_notional = ctx.wallet_total_usdt * max_pct / 100.0

    if proposal.notional <= max_notional:
        return CheckResult(
            CHECK_POSITION_SIZE, True, ACTION_PASS,
            f"Notional {proposal.notional:.2f} USDT within "
            f"{max_pct}% limit ({max_notional:.2f} USDT).",
        )

    if max_notional < min_notional:
        return CheckResult(
            CHECK_POSITION_SIZE, False, ACTION_REJECT,
            f"Resized notional {max_notional:.2f} USDT is below the minimum "
            f"viable order ({min_notional:.2f} USDT).",
        )

    return CheckResult(
        CHECK_POSITION_SIZE, False, ACTION_RESIZE,
        f"Notional {proposal.notional:.2f} USDT exceeds {max_pct}% limit; "
        f"resized to {max_notional:.2f} USDT.",
        suggested_quantity=max_notional / proposal.price,
    )


def check_exposure(
    proposal: TradeProposal, ctx: RiskContext, limits: dict
) -> CheckResult:
    """Total exposure across all symbols must stay <= max_total_exposure_pct,
    leaving a cash buffer. Resizes to the remaining headroom (§6)."""
    if not proposal.increases_risk:
        return CheckResult(
            CHECK_EXPOSURE, True, ACTION_PASS,
            "Sell order reduces total exposure.",
        )

    max_pct = limits["max_total_exposure_pct"]
    min_notional = limits["min_order_notional_usdt"]

    if ctx.wallet_total_usdt <= 0:
        return CheckResult(
            CHECK_EXPOSURE, False, ACTION_REJECT,
            f"Wallet total is {ctx.wallet_total_usdt:.2f} USDT; no exposure headroom.",
        )

    exposure_cap = ctx.wallet_total_usdt * max_pct / 100.0
    headroom = exposure_cap - ctx.current_exposure_usdt

    if proposal.notional <= headroom:
        return CheckResult(
            CHECK_EXPOSURE, True, ACTION_PASS,
            f"Exposure would be "
            f"{(ctx.current_exposure_usdt + proposal.notional):.2f}/"
            f"{exposure_cap:.2f} USDT ({max_pct}% cap).",
        )

    if headroom < min_notional:
        return CheckResult(
            CHECK_EXPOSURE, False, ACTION_REJECT,
            f"Exposure headroom {headroom:.2f} USDT is below the minimum "
            f"viable order ({min_notional:.2f} USDT); already at "
            f"{ctx.current_exposure_usdt:.2f}/{exposure_cap:.2f} USDT.",
        )

    return CheckResult(
        CHECK_EXPOSURE, False, ACTION_RESIZE,
        f"Notional {proposal.notional:.2f} USDT exceeds remaining headroom; "
        f"resized to {headroom:.2f} USDT ({max_pct}% total exposure cap).",
        suggested_quantity=headroom / proposal.price,
    )


def check_daily_loss(
    proposal: TradeProposal, ctx: RiskContext, limits: dict
) -> CheckResult:
    """Reject new risk once today's loss reaches max_daily_loss_pct (§6).

    Exits stay permitted: blocking a sell during a drawdown would trap the
    system in the very position that caused it.
    """
    if not proposal.increases_risk:
        return CheckResult(
            CHECK_DAILY_LOSS, True, ACTION_PASS,
            "Sell order permitted regardless of daily loss (exit, not new risk).",
        )

    max_loss = limits["max_daily_loss_pct"]
    pnl = ctx.daily_pnl

    # Absent baseline is not a healthy baseline (§1.7).
    if not pnl.available:
        return CheckResult(
            CHECK_DAILY_LOSS, False, ACTION_REJECT,
            f"Cannot establish today's P&L baseline: {pnl.reason}",
        )

    if pnl.pnl_pct <= -abs(max_loss):
        return CheckResult(
            CHECK_DAILY_LOSS, False, ACTION_REJECT,
            f"Daily loss {pnl.pnl_pct:.2f}% has reached the "
            f"{-abs(max_loss):.2f}% cap; no new positions until the next UTC day.",
        )

    return CheckResult(
        CHECK_DAILY_LOSS, True, ACTION_PASS,
        f"Daily P&L {pnl.pnl_pct:+.2f}% within the {-abs(max_loss):.2f}% cap.",
    )


def check_confidence(
    proposal: TradeProposal, ctx: RiskContext, limits: dict
) -> CheckResult:
    """Model probability must exceed min_confidence (§6).

    A buy with no confidence supplied is rejected rather than skipped —
    otherwise omitting the field would bypass the floor entirely. Sells are
    exempt: exit orders come from stop/position logic, not the classifier,
    which only ever predicts up-moves.
    """
    min_confidence = limits["min_confidence"]

    if proposal.confidence is None:
        if proposal.increases_risk:
            return CheckResult(
                CHECK_CONFIDENCE, False, ACTION_REJECT,
                "Buy order carries no model confidence; refusing to open a "
                "position without one.",
            )
        return CheckResult(
            CHECK_CONFIDENCE, True, ACTION_PASS,
            "Sell order needs no model confidence (exit order).",
        )

    if proposal.confidence < min_confidence:
        return CheckResult(
            CHECK_CONFIDENCE, False, ACTION_REJECT,
            f"Model confidence {proposal.confidence:.4f} is below the "
            f"{min_confidence} floor.",
        )

    return CheckResult(
        CHECK_CONFIDENCE, True, ACTION_PASS,
        f"Model confidence {proposal.confidence:.4f} meets the "
        f"{min_confidence} floor.",
    )


def check_volatility(
    proposal: TradeProposal, ctx: RiskContext, limits: dict
) -> CheckResult:
    """Recent candle range and volume must be within historical bounds (§6).

    A spike beyond the sigma limit suggests a flash event or a data glitch;
    either way the price feeding this decision cannot be trusted. Applies to
    every order, including sells.
    """
    sigma_limit = limits["volatility_sigma_limit"]
    stats = ctx.volatility

    if not stats.available:
        return CheckResult(
            CHECK_VOLATILITY, False, ACTION_REJECT,
            f"Cannot assess volatility: {stats.reason}",
        )

    breaches = []
    if abs(stats.range_z) > sigma_limit:
        breaches.append(f"candle range {stats.range_z:+.2f} sigma")
    if abs(stats.volume_z) > sigma_limit:
        breaches.append(f"volume {stats.volume_z:+.2f} sigma")

    if breaches:
        return CheckResult(
            CHECK_VOLATILITY, False, ACTION_REJECT,
            f"Abnormal market data ({'; '.join(breaches)}) exceeds the "
            f"{sigma_limit} sigma limit — possible flash event or data "
            f"glitch. Flagged for manual review.",
        )

    return CheckResult(
        CHECK_VOLATILITY, True, ACTION_PASS,
        f"Candle range {stats.range_z:+.2f} sigma and volume "
        f"{stats.volume_z:+.2f} sigma within the {sigma_limit} sigma limit.",
    )


def check_component_health(
    proposal: TradeProposal, ctx: RiskContext, limits: dict
) -> CheckResult:
    """Required components must be online with a recent heartbeat (§6).

    Only *dependencies* are checked — `risk_engine` never gates on its own
    heartbeat. Doing so would be both meaningless (this code executing is
    itself proof the engine is alive) and a deadlock risk, since the
    heartbeat is written by the scheduler: a scheduler stall would silently
    halt trading with a confusing cause. The engine records its own
    heartbeat as a side effect for the dashboard, never as a gate.
    """
    required = limits["required_healthy_components"]
    max_age = timedelta(seconds=limits["component_heartbeat_max_age_seconds"])

    problems = []
    for name in required:
        heartbeat = ctx.components.get(name)

        # A missing row is not a healthy one (§1.7).
        if heartbeat is None:
            problems.append(f"{name}: no status recorded")
            continue

        if heartbeat.status != "online":
            problems.append(f"{name}: {heartbeat.status}")
            continue

        if heartbeat.last_heartbeat is None:
            problems.append(f"{name}: no heartbeat timestamp")
            continue

        age = ctx.now - heartbeat.last_heartbeat
        if age > max_age:
            problems.append(
                f"{name}: heartbeat {age.total_seconds():.0f}s old "
                f"(max {max_age.total_seconds():.0f}s)"
            )

    if problems:
        return CheckResult(
            CHECK_HEALTH, False, ACTION_REJECT,
            f"Component health failure — {'; '.join(problems)}.",
        )

    return CheckResult(
        CHECK_HEALTH, True, ACTION_PASS,
        f"All required components online: {', '.join(required)}.",
    )


def check_trade_frequency(
    proposal: TradeProposal, ctx: RiskContext, limits: dict
) -> CheckResult:
    """Daily trade count cap.

    Not one of §6's six rules, but `max_trades_per_day` is config-driven per
    §11 and would otherwise go unenforced.

    Only executed trades count toward the cap — never proposals the engine
    rejected. Six rejections for unrelated reasons must not silently exhaust
    the cap and later read as "hit the daily trade limit". The detail string
    is deliberately prefixed so this rejection is distinguishable from every
    other one in risk_log.
    """
    if not proposal.increases_risk:
        return CheckResult(
            CHECK_FREQUENCY, True, ACTION_PASS,
            "frequency cap: not applied to sell orders (exits always allowed).",
        )

    max_trades = limits["max_trades_per_day"]

    if ctx.trades_today >= max_trades:
        return CheckResult(
            CHECK_FREQUENCY, False, ACTION_REJECT,
            f"frequency cap: {ctx.trades_today}/{max_trades} executed trades "
            f"today; no further entries until the next UTC day.",
        )

    return CheckResult(
        CHECK_FREQUENCY, True, ACTION_PASS,
        f"frequency cap: {ctx.trades_today}/{max_trades} executed trades today.",
    )


# Order here is the order they appear in risk_log.checks.
ALL_RULES = (
    check_component_health,
    check_volatility,
    check_confidence,
    check_daily_loss,
    check_position_size,
    check_exposure,
    check_trade_frequency,
)


def evaluate(
    proposal: TradeProposal, ctx: RiskContext, limits: dict
) -> RiskDecision:
    """Run every rule and combine the results.

    Deliberately does not short-circuit: a rejected trade should record all
    of its objections, not just the first, or risk_log is useless for
    working out what actually happened.
    """
    results = [rule(proposal, ctx, limits) for rule in ALL_RULES]

    rejections = [r for r in results if r.action == ACTION_REJECT]
    resizes = [r for r in results if r.action == ACTION_RESIZE]

    if rejections:
        return RiskDecision(
            decision=DECISION_REJECTED,
            reason=" | ".join(f"{r.name}: {r.detail}" for r in rejections),
            checks=results,
            final_quantity=0.0,
            original_quantity=proposal.quantity,
        )

    if resizes:
        # Several rules may each cap the size; the tightest one wins.
        final_quantity = min(r.suggested_quantity for r in resizes)
        return RiskDecision(
            decision=DECISION_RESIZED,
            reason=" | ".join(f"{r.name}: {r.detail}" for r in resizes),
            checks=results,
            final_quantity=final_quantity,
            original_quantity=proposal.quantity,
        )

    return RiskDecision(
        decision=DECISION_APPROVED,
        reason="All risk checks passed.",
        checks=results,
        final_quantity=proposal.quantity,
        original_quantity=proposal.quantity,
    )


# --------------------------------------------------------------------------
# I/O layer: gather context, persist decisions.
# --------------------------------------------------------------------------

LIMIT_KEYS = (
    "max_position_pct",
    "max_daily_loss_pct",
    "min_confidence",
    "max_total_exposure_pct",
    "volatility_sigma_limit",
    "component_heartbeat_max_age_seconds",
    "min_order_notional_usdt",
    "max_trades_per_day",
    "required_healthy_components",
    "volatility_lookback_candles",
    "max_pnl_baseline_age_hours",
)


async def load_limits(db: AsyncSession) -> dict:
    """Read every risk threshold from the config table (§1.1)."""
    return {key: await get_config(db, key) for key in LIMIT_KEYS}


async def get_daily_pnl(
    db: AsyncSession, stage: str, now: datetime, max_baseline_age_hours: float
) -> DailyPnlBaseline:
    """Today's account change against the day-open wallet value.

    The baseline is the most recent snapshot at or before today's UTC start —
    the true day-open value — rather than today's *first* snapshot, which
    would miss any move that happened before the bot started this morning.

    A baseline far older than the day boundary means the bot was down across
    one or more days; measuring "today's" change against it would be wrong,
    so it is reported unavailable rather than silently misleading.
    """
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    baseline_row = (
        await db.execute(
            select(WalletSnapshot)
            .where(
                WalletSnapshot.stage == stage,
                WalletSnapshot.snapshot_at <= start_of_day,
            )
            .order_by(WalletSnapshot.snapshot_at.desc())
            .limit(1)
        )
    ).scalars().first()

    current_row = (
        await db.execute(
            select(WalletSnapshot)
            .where(WalletSnapshot.stage == stage)
            .order_by(WalletSnapshot.snapshot_at.desc())
            .limit(1)
        )
    ).scalars().first()

    if current_row is None:
        return DailyPnlBaseline(
            available=False,
            reason=(
                f"no wallet snapshot recorded for stage '{stage}' — take a "
                f"snapshot before trading"
            ),
        )

    if baseline_row is None:
        # First day of operation: fall back to the earliest snapshot today.
        baseline_row = (
            await db.execute(
                select(WalletSnapshot)
                .where(
                    WalletSnapshot.stage == stage,
                    WalletSnapshot.snapshot_at > start_of_day,
                )
                .order_by(WalletSnapshot.snapshot_at.asc())
                .limit(1)
            )
        ).scalars().first()

        if baseline_row is None:
            return DailyPnlBaseline(
                available=False,
                reason=f"no wallet snapshot available for stage '{stage}'",
            )

    age = start_of_day - baseline_row.snapshot_at
    if age > timedelta(hours=max_baseline_age_hours):
        return DailyPnlBaseline(
            available=False,
            baseline_at=baseline_row.snapshot_at,
            reason=(
                f"day-open baseline is stale — most recent snapshot before "
                f"today is {age.total_seconds() / 3600:.1f}h older than the "
                f"day boundary (max {max_baseline_age_hours}h), suggesting a "
                f"multi-day gap in operation"
            ),
        )

    baseline_value = float(baseline_row.total_value_usdt)
    current_value = float(current_row.total_value_usdt)

    if baseline_value <= 0:
        return DailyPnlBaseline(
            available=False,
            baseline_at=baseline_row.snapshot_at,
            reason=f"day-open wallet value is {baseline_value:.2f}; cannot compute a percentage",
        )

    pnl_pct = (current_value - baseline_value) / baseline_value * 100.0

    return DailyPnlBaseline(
        available=True,
        pnl_pct=pnl_pct,
        baseline_at=baseline_row.snapshot_at,
        baseline_value=baseline_value,
        current_value=current_value,
    )


def zscore_latest(values: list[float]) -> float:
    """Z-score of values[0] against values[1:] (newest first).

    Returns 0.0 for a flat history that the latest value matches, and
    infinity for one it does not.

    The tolerance matters: summing N identical floats and dividing by N
    rarely reproduces the value exactly, so `std` comes out tiny-but-nonzero
    rather than zero. Dividing by it collapses to exactly +/-1 sigma, which
    would report a phantom one-sigma move on a perfectly flat market.
    """
    latest = values[0]
    history = values[1:]

    mean = sum(history) / len(history)
    variance = sum((v - mean) ** 2 for v in history) / len(history)
    std = variance ** 0.5

    tolerance = max(abs(mean), 1e-12) * 1e-9
    if std <= tolerance:
        return 0.0 if abs(latest - mean) <= tolerance else float("inf")

    return (latest - mean) / std


async def get_volatility_stats(
    db: AsyncSession, symbol: str, interval: str, lookback: int
) -> VolatilityStats:
    """Z-score the latest candle's range and volume against recent history."""
    rows = (
        await db.execute(
            select(Candle.high, Candle.low, Candle.close, Candle.volume)
            .where(Candle.symbol == symbol, Candle.interval == interval)
            .order_by(Candle.open_time.desc())
            .limit(lookback)
        )
    ).all()

    # Need enough history for a standard deviation to mean anything.
    if len(rows) < 20:
        return VolatilityStats(
            available=False,
            sample_size=len(rows),
            reason=(
                f"only {len(rows)} candles for {symbol} {interval}; "
                f"need at least 20 to establish normal bounds"
            ),
        )

    ranges = []
    volumes = []
    for high, low, close, volume in rows:
        close = float(close)
        if close <= 0:
            continue
        ranges.append((float(high) - float(low)) / close)
        volumes.append(float(volume))

    return VolatilityStats(
        available=True,
        range_z=zscore_latest(ranges),
        volume_z=zscore_latest(volumes),
        sample_size=len(rows),
    )


async def count_trades_today(db: AsyncSession, stage: str, now: datetime) -> int:
    """Executed trades since the UTC day boundary.

    Counts only trades that actually took a position — see
    CAP_CONSUMING_STATUSES. Rejected proposals never reach `trades` at all.
    """
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.count(Trade.id)).where(
            Trade.stage == stage,
            Trade.created_at >= start_of_day,
            Trade.status.in_(CAP_CONSUMING_STATUSES),
        )
    )
    return int(result.scalar_one() or 0)


async def get_component_heartbeats(db: AsyncSession) -> dict[str, ComponentHeartbeat]:
    rows = (await db.execute(select(ComponentStatus))).scalars().all()
    return {
        row.component: ComponentHeartbeat(row.status, row.last_heartbeat)
        for row in rows
    }


async def build_context(
    db: AsyncSession,
    symbol: str,
    stage: str,
    wallet_total_usdt: float,
    current_exposure_usdt: float,
    limits: dict,
    now: datetime | None = None,
) -> RiskContext:
    """Gather all state the rules need. The only I/O in this module's path."""
    now = now or datetime.now(timezone.utc)
    interval = await get_config(db, "interval")

    return RiskContext(
        now=now,
        wallet_total_usdt=wallet_total_usdt,
        current_exposure_usdt=current_exposure_usdt,
        daily_pnl=await get_daily_pnl(
            db, stage, now, limits["max_pnl_baseline_age_hours"]
        ),
        volatility=await get_volatility_stats(
            db, symbol, interval, limits["volatility_lookback_candles"]
        ),
        components=await get_component_heartbeats(db),
        trades_today=await count_trades_today(db, stage, now),
    )


async def log_decision(
    db: AsyncSession,
    proposal: TradeProposal,
    decision: RiskDecision,
    ctx: RiskContext,
    trade_id=None,
) -> RiskLog:
    """Write the decision to risk_log — every attempt, not just rejections
    (§3, §6). Caller commits.

    The proposal snapshot is embedded in `checks` because a rejected attempt
    never becomes a `trades` row, so `trade_id` is NULL and there would
    otherwise be no record of what was proposed.
    """
    entry = RiskLog(
        trade_id=trade_id,
        checks={
            "proposal": proposal.snapshot(),
            "context": {
                "stage_wallet_total_usdt": ctx.wallet_total_usdt,
                "current_exposure_usdt": ctx.current_exposure_usdt,
                "daily_pnl_pct": ctx.daily_pnl.pnl_pct,
                "daily_pnl_available": ctx.daily_pnl.available,
                "trades_today": ctx.trades_today,
                "evaluated_at": ctx.now.isoformat(),
            },
            "results": decision.checks_dict(),
            "final_quantity": decision.final_quantity,
            "original_quantity": decision.original_quantity,
        },
        decision=decision.decision,
        reason=decision.reason,
    )
    db.add(entry)
    await db.flush()

    log = logger.warning if decision.decision == DECISION_REJECTED else logger.info
    log(
        "Risk %s for %s %s %.8f @ %.2f: %s",
        decision.decision, proposal.side, proposal.symbol,
        proposal.quantity, proposal.price, decision.reason,
    )
    return entry


async def assess(
    db: AsyncSession,
    proposal: TradeProposal,
    stage: str,
    wallet_total_usdt: float,
    current_exposure_usdt: float,
    now: datetime | None = None,
) -> tuple[RiskDecision, RiskLog]:
    """Full path: gather context, run the rules, log the outcome.

    Caller commits. Returns the decision and its risk_log row so the trading
    engine can link the log to a trade once one is created.
    """
    limits = await load_limits(db)
    ctx = await build_context(
        db, proposal.symbol, stage, wallet_total_usdt,
        current_exposure_usdt, limits, now,
    )
    decision = evaluate(proposal, ctx, limits)
    entry = await log_decision(db, proposal, decision, ctx)
    return decision, entry
