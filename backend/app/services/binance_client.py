"""Binance REST wrapper (§4, §5.2).

Two distinct concerns, deliberately not the same client:

* **Market data** (klines, tickers) is public and identical regardless of
  stage, and is always read from production. The testnet has only synthetic,
  sparse price history, so training on it would be training on noise.
* **Trading** (orders, balances) is stage-gated: testnet while paper,
  production once live. Same code path either way — only the flag differs,
  per §5.2.
"""

import logging
import time

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from app.config import settings

logger = logging.getLogger(__name__)

# -1003 is Binance's "too many requests" code — distinct from a hard failure,
# it means back off and retry, not give up. python-binance's own pagination
# already sleeps 1s every 3rd call within a single symbol, but that alone
# isn't enough once several symbols' weight adds up against the shared
# per-IP budget; this is the backstop for when it still gets hit.
RATE_LIMIT_CODE = -1003
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = (5, 15, 30)


class BinanceClientError(RuntimeError):
    """Any failure reaching Binance, normalised for the caller.

    §1.7: callers surface this loudly and stop trading rather than acting on
    stale or absent data.
    """


class BinanceAPIClient:
    """Thin wrapper over python-binance.

    The underlying client is created lazily: constructing one performs a
    network round-trip, and building it eagerly at import time would make
    the whole app fail to start whenever Binance is unreachable.
    """

    # python-binance sits on `requests`, which has no default timeout — a
    # stalled connection would otherwise block for as long as the OS TCP
    # stack allows (observed: over a minute), and since every call site in
    # this app is synchronous, that stall freezes the whole asyncio event
    # loop, not just the caller. Every request gets this ceiling instead.
    REQUEST_TIMEOUT_SECONDS = 10

    def __init__(self, testnet: bool = False):
        self.testnet = testnet
        self.api_key = settings.BINANCE_API_KEY
        self.api_secret = settings.BINANCE_API_SECRET
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            try:
                self._client = Client(
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    testnet=self.testnet,
                    requests_params={"timeout": self.REQUEST_TIMEOUT_SECONDS},
                )
            except (BinanceAPIException, BinanceRequestException, OSError) as exc:
                raise BinanceClientError(
                    f"Could not connect to Binance "
                    f"({'testnet' if self.testnet else 'production'}): {exc}"
                ) from exc
        return self._client

    def get_historical_klines(
        self, symbol: str, interval: str, start_str: str, end_str: str | None = None
    ) -> list:
        """Fetch klines, paginating backward until `start_str` is reached.

        python-binance handles the pagination and the 1000-candle page limit.
        A -1003 (rate limit) is retried with backoff rather than failing the
        whole symbol outright — everything else still fails immediately.
        """
        attempt = 0
        while True:
            try:
                return self.client.get_historical_klines(
                    symbol, interval, start_str, end_str
                )
            except BinanceAPIException as exc:
                if exc.code != RATE_LIMIT_CODE or attempt >= RATE_LIMIT_MAX_RETRIES:
                    raise BinanceClientError(
                        f"Failed fetching klines for {symbol} {interval}: {exc}"
                    ) from exc
                wait = RATE_LIMIT_BACKOFF_SECONDS[
                    min(attempt, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)
                ]
                logger.warning(
                    "Rate limited (-1003) fetching %s %s; retrying in %ds "
                    "(attempt %d/%d).",
                    symbol, interval, wait, attempt + 1, RATE_LIMIT_MAX_RETRIES,
                )
                time.sleep(wait)
                attempt += 1
            except (BinanceRequestException, OSError) as exc:
                raise BinanceClientError(
                    f"Failed fetching klines for {symbol} {interval}: {exc}"
                ) from exc

    def get_symbol_price(self, symbol: str) -> float:
        """Latest trade price for a symbol."""
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker["price"])
        except (BinanceAPIException, BinanceRequestException, OSError, KeyError) as exc:
            raise BinanceClientError(
                f"Failed fetching price for {symbol}: {exc}"
            ) from exc

    def get_server_time(self) -> dict:
        """Used as the connectivity heartbeat for `component_status`."""
        try:
            return self.client.get_server_time()
        except (BinanceAPIException, BinanceRequestException, OSError) as exc:
            raise BinanceClientError(f"Binance unreachable: {exc}") from exc


def get_market_data_client() -> BinanceAPIClient:
    """Public market data — always production, never testnet (see module doc)."""
    return BinanceAPIClient(testnet=False)


def get_trading_client(stage: str) -> BinanceAPIClient:
    """Order-placing client for the given stage.

    Anything that is not an explicit `live` stage gets the testnet, so a
    misconfigured or unrecognised stage value can never place real orders.
    """
    return BinanceAPIClient(testnet=(stage != "live"))
