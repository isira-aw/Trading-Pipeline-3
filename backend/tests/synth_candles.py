"""Synthetic OHLCV generator for exercising the pipeline offline.

This exists so the training/registry/risk code can be run end-to-end without
network access to Binance. It produces a plausibly-shaped series (random
walk with volatility clustering), NOT real market data.

Metrics produced from this data say only that the code runs — they carry no
information about whether the strategy works. Real evaluation requires a
real download.
"""

from datetime import datetime, timedelta, timezone

import numpy as np

INTERVAL_MINUTES = {"1h": 60, "4h": 240, "1d": 1440}


def generate_klines(
    count: int = 3000,
    interval: str = "4h",
    start_price: float = 40000.0,
    seed: int = 7,
    end: datetime | None = None,
) -> list[list]:
    """Return Binance-shaped kline rows (positional arrays, string prices)."""
    rng = np.random.default_rng(seed)
    step_minutes = INTERVAL_MINUTES[interval]

    if end is None:
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(minutes=step_minutes * count)

    # GARCH-ish volatility clustering: vol mean-reverts but shocks persist.
    volatility = np.empty(count)
    volatility[0] = 0.012
    shocks = rng.normal(0.0, 1.0, count)
    for i in range(1, count):
        volatility[i] = np.clip(
            0.0002 + 0.90 * volatility[i - 1] + 0.05 * abs(shocks[i - 1]) * 0.01,
            0.003,
            0.06,
        )

    returns = shocks * volatility
    closes = start_price * np.exp(np.cumsum(returns))

    klines = []
    for i in range(count):
        close = closes[i]
        open_ = closes[i - 1] if i > 0 else start_price
        # Wick sizes scale with that bar's volatility.
        span = abs(close) * volatility[i]
        high = max(open_, close) + abs(rng.normal(0, span))
        low = min(open_, close) - abs(rng.normal(0, span))
        volume = float(abs(rng.normal(1200, 400)) + 50)

        open_time = start + timedelta(minutes=step_minutes * i)
        klines.append(
            [
                int(open_time.timestamp() * 1000),
                f"{open_:.2f}", f"{high:.2f}", f"{low:.2f}", f"{close:.2f}",
                f"{volume:.4f}",
                int((open_time + timedelta(minutes=step_minutes)).timestamp() * 1000),
                "0", 0, "0", "0", "0",
            ]
        )

    return klines
