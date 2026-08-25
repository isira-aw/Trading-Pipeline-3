"""Tests for FIFO position matching and realized P&L (§5.2, §5.4).

Expected values here are computed by hand in the comments. These numbers
feed the promotion gate that decides whether real money gets deployed, so
"the code says so" is not an acceptable oracle.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.position_tracker import (
    TradeRecord,
    compute_stats,
    match_fifo,
    max_drawdown_pct,
)

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


def trade(n, side, qty, price, fee=0.0, symbol="BTCUSDT", model="model-a"):
    return TradeRecord(
        id=f"t{n}", symbol=symbol, side=side, quantity=qty, price=price,
        created_at=T0 + timedelta(hours=n), model_id=model, fee_usdt=fee,
    )


class TestSimpleMatching:
    def test_single_round_trip_profit(self):
        # Buy 1 @ 100, sell 1 @ 110 -> +10 gross, no fees.
        result = match_fifo([trade(1, "buy", 1, 100), trade(2, "sell", 1, 110)])

        assert len(result.closed) == 1
        position = result.closed[0]
        assert position.gross_pnl == pytest.approx(10.0)
        assert position.realized_pnl == pytest.approx(10.0)
        assert position.realized_pnl_pct == pytest.approx(10.0)
        assert position.is_win
        assert not result.open_lots

    def test_single_round_trip_loss(self):
        result = match_fifo([trade(1, "buy", 1, 100), trade(2, "sell", 1, 90)])
        assert result.closed[0].realized_pnl == pytest.approx(-10.0)
        assert not result.closed[0].is_win

    def test_unclosed_buy_stays_open(self):
        result = match_fifo([trade(1, "buy", 2, 100)])
        assert not result.closed
        assert len(result.open_lots) == 1
        assert result.open_lots[0].remaining == 2

    def test_partial_close_leaves_remainder_open(self):
        # Buy 3 @ 100, sell 1 @ 120 -> one closed position of qty 1, 2 open.
        result = match_fifo([trade(1, "buy", 3, 100), trade(2, "sell", 1, 120)])

        assert len(result.closed) == 1
        assert result.closed[0].quantity == 1
        assert result.closed[0].realized_pnl == pytest.approx(20.0)
        assert result.open_lots[0].remaining == pytest.approx(2.0)


class TestFifoOrdering:
    def test_oldest_lot_closes_first(self):
        """FIFO, not LIFO: the sell must match the 100 lot, not the 200 one."""
        result = match_fifo([
            trade(1, "buy", 1, 100),
            trade(2, "buy", 1, 200),
            trade(3, "sell", 1, 150),
        ])

        assert len(result.closed) == 1
        # FIFO -> entry 100, +50. LIFO would have given entry 200, -50.
        assert result.closed[0].entry_price == 100
        assert result.closed[0].realized_pnl == pytest.approx(50.0)
        assert result.open_lots[0].price == 200

    def test_one_sell_spans_multiple_lots(self):
        # Buy 1@100, buy 1@200, sell 2@150.
        #   lot1: (150-100)*1 = +50
        #   lot2: (150-200)*1 = -50
        # Two closed positions, net zero, one win and one loss.
        result = match_fifo([
            trade(1, "buy", 1, 100),
            trade(2, "buy", 1, 200),
            trade(3, "sell", 2, 150),
        ])

        assert len(result.closed) == 2
        assert [p.realized_pnl for p in result.closed] == pytest.approx([50.0, -50.0])
        assert compute_stats(result.closed)["win_rate"] == pytest.approx(0.5)

    def test_input_order_does_not_matter(self):
        trades = [
            trade(3, "sell", 1, 150),
            trade(1, "buy", 1, 100),
            trade(2, "buy", 1, 200),
        ]
        assert match_fifo(trades).closed[0].entry_price == 100

    def test_symbols_are_matched_independently(self):
        """An ETH sell must never close a BTC lot."""
        result = match_fifo([
            trade(1, "buy", 1, 100, symbol="BTCUSDT"),
            trade(2, "buy", 1, 10, symbol="ETHUSDT"),
            trade(3, "sell", 1, 20, symbol="ETHUSDT"),
        ])

        assert len(result.closed) == 1
        assert result.closed[0].symbol == "ETHUSDT"
        assert result.closed[0].realized_pnl == pytest.approx(10.0)
        assert [lot.symbol for lot in result.open_lots] == ["BTCUSDT"]


class TestFees:
    def test_fees_reduce_realized_pnl(self):
        # Buy 1@100 (fee 0.1), sell 1@110 (fee 0.11).
        # gross +10, fees 0.21 -> realized 9.79.
        result = match_fifo([
            trade(1, "buy", 1, 100, fee=0.10),
            trade(2, "sell", 1, 110, fee=0.11),
        ])
        position = result.closed[0]
        assert position.gross_pnl == pytest.approx(10.0)
        assert position.fees == pytest.approx(0.21)
        assert position.realized_pnl == pytest.approx(9.79)

    def test_small_gain_that_does_not_cover_fees_is_a_loss(self):
        """The case that makes fee handling matter: a position that moved the
        right way but lost money after costs must not count as a win."""
        # Buy 1@100 (fee 0.1), sell 1@100.15 (fee 0.1) -> +0.15 gross, 0.2 fees.
        result = match_fifo([
            trade(1, "buy", 1, 100, fee=0.10),
            trade(2, "sell", 1, 100.15, fee=0.10),
        ])
        position = result.closed[0]
        assert position.gross_pnl > 0
        assert position.realized_pnl == pytest.approx(-0.05)
        assert not position.is_win

    def test_fees_allocated_proportionally_across_partial_closes(self):
        # Buy 4@100 with a 0.40 fee -> 0.10/unit. Sell 2@110 with 0.20 fee.
        # Closed qty 2: fees = 2*0.10 (entry) + 2*0.10 (exit) = 0.40.
        result = match_fifo([
            trade(1, "buy", 4, 100, fee=0.40),
            trade(2, "sell", 2, 110, fee=0.20),
        ])
        assert result.closed[0].fees == pytest.approx(0.40)
        assert result.closed[0].realized_pnl == pytest.approx(20.0 - 0.40)


class TestModelAttribution:
    def test_position_attributed_to_the_opening_model(self):
        """The buy carries the signal; the sell may be stop logic."""
        result = match_fifo([
            trade(1, "buy", 1, 100, model="model-a"),
            trade(2, "sell", 1, 110, model=None),
        ])
        assert result.closed[0].model_id == "model-a"

    def test_different_models_keep_separate_attribution(self):
        result = match_fifo([
            trade(1, "buy", 1, 100, model="model-a"),
            trade(2, "buy", 1, 100, model="model-b"),
            trade(3, "sell", 2, 120, model=None),
        ])
        assert [p.model_id for p in result.closed] == ["model-a", "model-b"]


class TestEdgeCases:
    def test_sell_without_any_open_lot_is_recorded_not_dropped(self):
        result = match_fifo([trade(1, "sell", 1, 100)])
        assert not result.closed
        assert result.unmatched_sell_quantity == pytest.approx(1.0)

    def test_oversized_sell_closes_what_it_can(self):
        result = match_fifo([trade(1, "buy", 1, 100), trade(2, "sell", 3, 110)])
        assert len(result.closed) == 1
        assert result.closed[0].quantity == pytest.approx(1.0)
        assert result.unmatched_sell_quantity == pytest.approx(2.0)

    def test_empty_input(self):
        result = match_fifo([])
        assert not result.closed and not result.open_lots

    def test_unknown_side_ignored(self):
        result = match_fifo([trade(1, "hold", 1, 100)])
        assert not result.closed and not result.open_lots

    def test_floating_point_residue_closes_the_lot(self):
        """0.1+0.2 style residue must not leave a phantom open lot."""
        result = match_fifo([
            trade(1, "buy", 0.3, 100),
            trade(2, "sell", 0.1, 110),
            trade(3, "sell", 0.2, 110),
        ])
        assert not result.open_lots


class TestComputeStats:
    def test_no_closed_positions_reports_none_win_rate(self):
        """None, not 0.0 — the scorer must distinguish "no data" from
        "everything lost"."""
        stats = compute_stats([])
        assert stats["win_rate"] is None
        assert stats["closed_trades"] == 0

    def test_known_sequence_win_rate(self):
        """Hand-computed: 5 round trips, 3 winners.

            b1 100 -> s 110  = +10  win
            b2 100 -> s  95  =  -5  loss
            b3 100 -> s 120  = +20  win
            b4 100 -> s  90  = -10  loss
            b5 100 -> s 105  =  +5  win

        wins 3 / 5 = 0.6 ; total = 10-5+20-10+5 = +20
        """
        exits = [110, 95, 120, 90, 105]
        trades = []
        for i, exit_price in enumerate(exits):
            trades.append(trade(i * 2 + 1, "buy", 1, 100))
            trades.append(trade(i * 2 + 2, "sell", 1, exit_price))

        stats = compute_stats(match_fifo(trades).closed)

        assert stats["closed_trades"] == 5
        assert stats["wins"] == 3
        assert stats["losses"] == 2
        assert stats["win_rate"] == pytest.approx(0.6)
        assert stats["total_realized_pnl"] == pytest.approx(20.0)
        # avg win = (10+20+5)/3 = 11.666..., avg loss = (-5-10)/2 = -7.5
        assert stats["avg_win"] == pytest.approx(35.0 / 3.0)
        assert stats["avg_loss"] == pytest.approx(-7.5)

    def test_win_rate_is_net_of_fees(self):
        """Same price sequence, but fees flip one winner into a loser.

        b 100 -> s 100.05 with 0.05 fees each side: gross +0.05, fees 0.10,
        realized -0.05. Gross would say 1.0 win rate; net says 0.0.
        """
        trades = [
            trade(1, "buy", 1, 100, fee=0.05),
            trade(2, "sell", 1, 100.05, fee=0.05),
        ]
        stats = compute_stats(match_fifo(trades).closed)
        assert stats["gross_pnl"] > 0
        assert stats["total_realized_pnl"] < 0
        assert stats["win_rate"] == pytest.approx(0.0)


class TestMaxDrawdown:
    def test_monotonic_gains_have_no_drawdown(self):
        trades = []
        for i in range(3):
            trades.append(trade(i * 2 + 1, "buy", 1, 100))
            trades.append(trade(i * 2 + 2, "sell", 1, 110))
        assert max_drawdown_pct(match_fifo(trades).closed) == pytest.approx(0.0)

    def test_drawdown_measured_from_peak(self):
        """+100 then -30: peak 100, trough 70 -> 30% drawdown."""
        trades = [
            trade(1, "buy", 1, 100), trade(2, "sell", 1, 200),   # +100
            trade(3, "buy", 1, 100), trade(4, "sell", 1, 70),    # -30
        ]
        assert max_drawdown_pct(match_fifo(trades).closed) == pytest.approx(30.0)

    def test_recovery_keeps_the_worst_drawdown(self):
        trades = [
            trade(1, "buy", 1, 100), trade(2, "sell", 1, 200),   # +100 peak
            trade(3, "buy", 1, 100), trade(4, "sell", 1, 50),    # -50 -> 50
            trade(5, "buy", 1, 100), trade(6, "sell", 1, 300),   # +200 -> 250
        ]
        # Worst point: 50 against a peak of 100 -> 50%.
        assert max_drawdown_pct(match_fifo(trades).closed) == pytest.approx(50.0)

    def test_empty_is_zero(self):
        assert max_drawdown_pct([]) == 0.0
