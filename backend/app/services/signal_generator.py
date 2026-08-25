"""Turn the active model into a trade signal (§5.1, §5.2).

The model outputs a probability that price rises by the configured amount
within the configured horizon. That probability is passed to the risk
engine as `confidence` — it is not a price prediction and nothing here
decides whether to trade. The risk engine's confidence floor does.

Entries come from the model. Exits do not: the classifier only ever
predicts up-moves, so it has nothing to say about when to close. Exits are
decided by `evaluate_exits` on the position's own terms — target reached,
or the prediction horizon elapsed without it.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.services.config_service import get_config
from app.services.features import FEATURE_COLUMNS, MIN_CANDLES_FOR_FEATURES, compute_features
from app.services.model_registry import ModelRegistryError, load_active_model
from app.services.training_pipeline import load_candles

logger = logging.getLogger(__name__)

# Interval -> minutes, for turning a candle horizon into a wall-clock age.
INTERVAL_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
}


@dataclass
class Signal:
    symbol: str
    confidence: float | None
    price: float
    model_id: str | None
    candle_time: datetime | None
    available: bool
    reason: str | None = None


async def generate_signal(db, symbol: str) -> Signal:
    """Score the most recent closed candle with the active model.

    Returns `available=False` rather than raising when there is no model or
    not enough data — the caller records that and skips the symbol, instead
    of trading on an absent signal (§1.7).
    """
    interval = await get_config(db, "interval")

    try:
        record, model = await load_active_model(db, symbol)
    except ModelRegistryError as exc:
        return Signal(symbol, None, 0.0, None, None, False, str(exc))

    candles = await load_candles(db, symbol, interval)
    if len(candles) < MIN_CANDLES_FOR_FEATURES + 1:
        return Signal(
            symbol, None, 0.0, str(record.id), None, False,
            f"Only {len(candles)} candles for {symbol} {interval}; need at "
            f"least {MIN_CANDLES_FOR_FEATURES + 1} to compute features.",
        )

    featured = compute_features(candles)
    latest = featured.iloc[[-1]]

    if latest[FEATURE_COLUMNS].isna().any(axis=None):
        return Signal(
            symbol, None, float(latest["close"].iloc[0]), str(record.id), None, False,
            "Latest candle still has incomplete indicator warm-up.",
        )

    probability = float(model.predict_proba(latest[FEATURE_COLUMNS])[0, 1])

    return Signal(
        symbol=symbol,
        confidence=probability,
        price=float(latest["close"].iloc[0]),
        model_id=str(record.id),
        candle_time=latest["open_time"].iloc[0].to_pydatetime(),
        available=True,
    )


def horizon_expiry(
    opened_at: datetime, interval: str, horizon_candles: int
) -> datetime:
    """When a position's prediction window has elapsed."""
    minutes = INTERVAL_MINUTES.get(interval, 240) * horizon_candles
    return opened_at + timedelta(minutes=minutes)


# The three ways a position can close, recorded on the closing trade.
EXIT_STOP = "stop_hit"
EXIT_TARGET = "target_reached"
EXIT_HORIZON = "horizon_elapsed"


@dataclass
class ExitDecision:
    should_exit: bool
    quantity: float = 0.0
    reason: str = ""
    exit_reason: str | None = None


def evaluate_exit(
    entry_price: float,
    opened_at: datetime,
    remaining: float,
    current_price: float,
    interval: str,
    target_move_pct: float,
    horizon_candles: int,
    # Keyword-only: these were added after the first callers existed, and a
    # positional call would silently bind `now` to `stop_price` — comparing
    # a datetime against a price rather than failing loudly.
    *,
    stop_price: float | None = None,
    candle_high: float | None = None,
    candle_low: float | None = None,
    now: datetime | None = None,
) -> ExitDecision:
    """Decide whether an open lot should be closed, and why.

    Three exits, checked in this fixed order:

    1. **Stop hit** — price fell to the ATR-derived stop set at open.
    2. **Target reached** — the rise the model predicted.
    3. **Horizon elapsed** — the prediction window passed without either.

    **Stop is checked first and wins ties**, and that ordering is a
    deliberate, conservative choice. Within one candle we have only OHLC:
    when the low breached the stop *and* the high reached the target, the
    order they occurred in is unknowable. Resolving that ambiguity in favour
    of the target would systematically overstate performance — and the
    inflated win rate feeds the §5.4 promotion gate that decides whether
    real money gets deployed. Assuming the worse of two unknowable orderings
    is the only safe default.

    When `candle_high`/`candle_low` are supplied, the extremes are used, so
    a stop breached intra-candle is honoured even if the candle closed back
    above it. With only `current_price` the two are mutually exclusive
    anyway, since stop < entry < target.
    """
    now = now or datetime.now(timezone.utc)
    target_price = entry_price * (1.0 + target_move_pct / 100.0)

    low = candle_low if candle_low is not None else current_price
    high = candle_high if candle_high is not None else current_price

    # 1. Stop — checked before target; see docstring on tie resolution.
    if stop_price is not None and low <= stop_price:
        return ExitDecision(
            True, remaining,
            f"Stop hit: price {low:.8f} reached the stop at "
            f"{stop_price:.8f} (set from ATR at open).",
            EXIT_STOP,
        )

    # 2. Target.
    if high >= target_price:
        return ExitDecision(
            True, remaining,
            f"Target reached: {high:.8f} >= {target_price:.8f} "
            f"({target_move_pct}% above entry).",
            EXIT_TARGET,
        )

    # 3. Horizon.
    expiry = horizon_expiry(opened_at, interval, horizon_candles)
    if now >= expiry:
        return ExitDecision(
            True, remaining,
            f"Prediction horizon elapsed at {expiry.isoformat()} without "
            f"reaching {target_price:.8f}; closing at {current_price:.8f}.",
            EXIT_HORIZON,
        )

    return ExitDecision(False, 0.0, "Position still within its horizon.")
