"""Unit tests for the trading engine's pure helpers (§5.2).

Fill parsing and lot rounding decide what quantity and price end up in the
`trades` row, which is what realized P&L is computed from — a mistake here
silently corrupts every performance number downstream.
"""

import pytest

from app.services.trading_engine import (
    BINANCE_STATUS_MAP,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_FILLED,
    STATUS_PARTIAL,
    STATUS_SUBMITTED,
    _price_assets,
    parse_fills,
    round_to_step,
)


class TestRoundToStep:
    def test_rounds_down_to_step(self):
        assert round_to_step(0.123456, 0.001) == pytest.approx(0.123)

    def test_never_rounds_up(self):
        """Rounding up would exceed the size the risk engine approved."""
        assert round_to_step(0.1999, 0.1) == pytest.approx(0.1)

    @pytest.mark.parametrize("qty,step", [
        (0.5, 0.1), (1.0, 0.1), (0.3, 0.1), (2.0, 0.5), (0.07, 0.01),
    ])
    def test_exact_multiple_unchanged(self, qty, step):
        """Regression: 0.5 // 0.1 == 4.0 in binary float, dropping a whole step."""
        assert round_to_step(qty, step) == pytest.approx(qty)

    def test_below_one_step_becomes_zero(self):
        assert round_to_step(0.0005, 0.001) == 0.0

    def test_zero_step_passes_through(self):
        assert round_to_step(0.123456, 0.0) == 0.123456


class TestParseFills:
    def test_simple_full_fill(self):
        response = {
            "symbol": "BTCUSDT", "status": "FILLED",
            "executedQty": "0.5", "cummulativeQuoteQty": "25000",
            "fills": [{"price": "50000", "qty": "0.5",
                       "commission": "25", "commissionAsset": "USDT"}],
        }
        qty, price, fee = parse_fills(response, 49000.0)
        assert qty == pytest.approx(0.5)
        assert price == pytest.approx(50000.0)
        assert fee == pytest.approx(25.0)

    def test_average_price_across_multiple_fills(self):
        """A market order walking the book fills at several prices; the
        recorded entry price must be the weighted average, not the last."""
        response = {
            "symbol": "BTCUSDT", "status": "FILLED",
            "executedQty": "2", "cummulativeQuoteQty": "202000",
            "fills": [
                {"price": "100000", "qty": "1", "commission": "0", "commissionAsset": "USDT"},
                {"price": "102000", "qty": "1", "commission": "0", "commissionAsset": "USDT"},
            ],
        }
        qty, price, _ = parse_fills(response, 0.0)
        assert qty == pytest.approx(2.0)
        assert price == pytest.approx(101000.0)

    def test_partial_fill_reports_executed_quantity(self):
        response = {
            "symbol": "BTCUSDT", "status": "PARTIALLY_FILLED",
            "executedQty": "0.3", "cummulativeQuoteQty": "15000", "fills": [],
        }
        qty, price, _ = parse_fills(response, 1.0)
        assert qty == pytest.approx(0.3)
        assert price == pytest.approx(50000.0)

    def test_unfilled_order_uses_fallback_price(self):
        response = {"symbol": "BTCUSDT", "status": "NEW",
                    "executedQty": "0", "cummulativeQuoteQty": "0", "fills": []}
        qty, price, fee = parse_fills(response, 49000.0)
        assert qty == 0.0
        assert price == pytest.approx(49000.0)
        assert fee == 0.0

    def test_commission_in_base_asset_converted_at_fill_price(self):
        """A BTC-denominated commission on BTCUSDT is worth qty * price."""
        response = {
            "symbol": "BTCUSDT", "status": "FILLED",
            "executedQty": "1", "cummulativeQuoteQty": "50000",
            "fills": [{"price": "50000", "qty": "1",
                       "commission": "0.001", "commissionAsset": "BTC"}],
        }
        _, _, fee = parse_fills(response, 0.0)
        assert fee == pytest.approx(50.0)

    def test_unconvertible_commission_excluded_not_guessed(self):
        """A BNB commission cannot be valued from this response alone.

        Excluding it understates fees slightly; inventing a rate would put a
        fabricated number into realized P&L, which is worse.
        """
        response = {
            "symbol": "BTCUSDT", "status": "FILLED",
            "executedQty": "1", "cummulativeQuoteQty": "50000",
            "fills": [{"price": "50000", "qty": "1",
                       "commission": "0.5", "commissionAsset": "BNB"}],
        }
        _, _, fee = parse_fills(response, 0.0)
        assert fee == 0.0

    def test_missing_fields_do_not_raise(self):
        qty, price, fee = parse_fills({}, 123.0)
        assert (qty, price, fee) == (0.0, 123.0, 0.0)


class TestStatusMapping:
    @pytest.mark.parametrize("binance,expected", [
        ("NEW", STATUS_SUBMITTED),
        ("PARTIALLY_FILLED", STATUS_PARTIAL),
        ("FILLED", STATUS_FILLED),
        ("CANCELED", STATUS_CANCELLED),
        ("REJECTED", STATUS_FAILED),
        ("EXPIRED", STATUS_FAILED),
    ])
    def test_maps_exchange_status(self, binance, expected):
        assert BINANCE_STATUS_MAP[binance] == expected


class TestAccountValuation:
    def test_quote_asset_counts_as_value_not_exposure(self):
        total, exposure = _price_assets({"USDT": 1000.0}, {})
        assert total == pytest.approx(1000.0)
        assert exposure == 0.0

    def test_holdings_valued_and_counted_as_exposure(self):
        total, exposure = _price_assets(
            {"USDT": 1000.0, "BTC": 0.02}, {"BTCUSDT": 50000.0}
        )
        assert total == pytest.approx(2000.0)
        assert exposure == pytest.approx(1000.0)

    def test_unpriceable_asset_excluded_rather_than_assumed_zero_or_one(self):
        total, exposure = _price_assets({"USDT": 500.0, "WEIRD": 10.0}, {})
        assert total == pytest.approx(500.0)
        assert exposure == 0.0

    def test_zero_balances_ignored(self):
        total, exposure = _price_assets({"USDT": 0.0, "BTC": 0.0}, {"BTCUSDT": 1.0})
        assert total == 0.0 and exposure == 0.0
