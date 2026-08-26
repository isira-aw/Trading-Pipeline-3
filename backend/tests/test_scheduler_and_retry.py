"""Tests for reconciliation retry/escalation, the trade-loop gate, exits and
the event bus.

The retry tests matter most: an order stuck in `submitted` means an unknown
position, and the failure mode being guarded against is that it sits there
unnoticed forever.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import trading_engine as te
from app.services.event_bus import EventBus
from app.services.signal_generator import evaluate_exit, horizon_expiry

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class FakeTrade:
    """Minimal stand-in with the attributes the retry helpers touch."""

    def __init__(self, risk_notes=None):
        self.id = uuid.uuid4()
        self.risk_notes = risk_notes


class TestBackoff:
    def test_doubles_each_attempt(self):
        assert [te.backoff_seconds(n, 30, 1800) for n in range(1, 5)] == [
            30, 60, 120, 240,
        ]

    def test_caps_at_maximum(self):
        assert te.backoff_seconds(20, 30, 1800) == 1800

    def test_first_attempt_uses_base(self):
        assert te.backoff_seconds(1, 30, 1800) == 30

    def test_zero_attempts_does_not_go_below_base(self):
        assert te.backoff_seconds(0, 30, 1800) == 30


class TestRetryScheduling:
    def test_never_attempted_order_is_due(self):
        assert te.is_due_for_retry(FakeTrade(), NOW)

    def test_order_inside_backoff_window_is_not_due(self):
        trade = FakeTrade({"reconcile": {
            "attempts": 1,
            "next_attempt_at": (NOW + timedelta(minutes=5)).isoformat(),
        }})
        assert not te.is_due_for_retry(trade, NOW)

    def test_order_past_its_window_is_due(self):
        trade = FakeTrade({"reconcile": {
            "attempts": 1,
            "next_attempt_at": (NOW - timedelta(seconds=1)).isoformat(),
        }})
        assert te.is_due_for_retry(trade, NOW)

    def test_escalated_order_stops_being_retried(self):
        """Once escalated it needs a human, not another automatic attempt."""
        trade = FakeTrade({"reconcile": {
            "attempts": 6,
            "needs_attention": True,
            "next_attempt_at": (NOW - timedelta(hours=1)).isoformat(),
        }})
        assert not te.is_due_for_retry(trade, NOW)

    def test_unrelated_risk_notes_do_not_break_scheduling(self):
        assert te.is_due_for_retry(FakeTrade({"reason": "something else"}), NOW)


class TestExitEvaluation:
    def test_target_reached_exits(self):
        decision = evaluate_exit(
            entry_price=100.0, opened_at=NOW, remaining=1.0,
            current_price=101.5, interval="4h", target_move_pct=1.0,
            horizon_candles=1, now=NOW + timedelta(minutes=10),
        )
        assert decision.should_exit
        assert "Target reached" in decision.reason

    def test_holds_inside_horizon_below_target(self):
        decision = evaluate_exit(
            100.0, NOW, 1.0, 100.5, "4h", 1.0, 1, now=NOW + timedelta(minutes=10)
        )
        assert not decision.should_exit

    def test_exits_when_horizon_elapsed(self):
        """The model claimed a move within N candles; past that its
        prediction no longer applies to the position."""
        decision = evaluate_exit(
            100.0, NOW, 1.0, 100.5, "4h", 1.0, 1, now=NOW + timedelta(hours=5)
        )
        assert decision.should_exit
        assert "horizon elapsed" in decision.reason

    def test_exit_closes_the_whole_remaining_lot(self):
        decision = evaluate_exit(
            100.0, NOW, 0.37, 105.0, "4h", 1.0, 1, now=NOW + timedelta(minutes=1)
        )
        assert decision.quantity == pytest.approx(0.37)

    def test_horizon_expiry_scales_with_interval(self):
        assert horizon_expiry(NOW, "4h", 1) == NOW + timedelta(hours=4)
        assert horizon_expiry(NOW, "1h", 3) == NOW + timedelta(hours=3)
        assert horizon_expiry(NOW, "1d", 2) == NOW + timedelta(days=2)

    def test_unknown_interval_falls_back_rather_than_raising(self):
        assert horizon_expiry(NOW, "7m", 1) == NOW + timedelta(minutes=240)


class TestEventBus:
    def test_subscriber_receives_published_event(self):
        bus = EventBus()
        queue = bus.subscribe()
        bus.publish("trade_event", {"symbol": "BTCUSDT"})

        message = queue.get_nowait()
        assert message["event"] == "trade_event"
        assert message["symbol"] == "BTCUSDT"
        assert "timestamp" in message

    def test_every_subscriber_receives_the_event(self):
        bus = EventBus()
        queues = [bus.subscribe() for _ in range(3)]
        bus.publish("system_event", {"message": "hi"})
        assert all(q.get_nowait()["message"] == "hi" for q in queues)

    def test_unsubscribed_queue_stops_receiving(self):
        bus = EventBus()
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        bus.publish("system_event", {})
        assert queue.empty()

    def test_publishing_with_no_subscribers_is_safe(self):
        EventBus().publish("trade_event", {})

    def test_saturated_subscriber_drops_oldest_and_never_blocks(self):
        """A browser tab that stops reading must not stall the trade loop."""
        bus = EventBus()
        queue = bus.subscribe()

        for i in range(150):  # QUEUE_SIZE is 100
            bus.publish("trade_event", {"n": i})

        assert queue.full()
        # Oldest were dropped; the newest event survived.
        drained = []
        while not queue.empty():
            drained.append(queue.get_nowait()["n"])
        assert drained[-1] == 149
        assert drained[0] > 0

    def test_publish_never_raises_on_a_broken_subscriber(self):
        bus = EventBus()

        class Exploding(asyncio.Queue):
            def put_nowait(self, item):
                raise RuntimeError("broken subscriber")

        bus._subscribers.add(Exploding())
        bus.publish("trade_event", {})  # must not raise

    def test_subscriber_count_tracks_connections(self):
        bus = EventBus()
        assert bus.subscriber_count == 0
        queue = bus.subscribe()
        assert bus.subscriber_count == 1
        bus.unsubscribe(queue)
        assert bus.subscriber_count == 0


class TestDownloadProgress:
    """Progress reporting for the WebSocket events and the poll fallback."""

    def setup_method(self):
        from app.services import data_downloader as dd
        dd._progress.update(
            {"running": False, "symbols": [], "completed": 0, "current": None}
        )

    def test_idle_progress_is_zero_not_a_division_error(self):
        from app.services.data_downloader import get_progress

        snapshot = get_progress()
        assert snapshot["total"] == 0
        assert snapshot["progress"] == 0.0
        assert snapshot["running"] is False

    def test_publish_updates_the_snapshot(self):
        from app.services.data_downloader import _publish, _progress, get_progress

        _progress.update({"symbols": ["A", "B", "C", "D"]})
        _publish("B", 1, 4, "downloading")

        snapshot = get_progress()
        assert snapshot["current"] == "B"
        assert snapshot["completed"] == 1
        assert snapshot["progress"] == pytest.approx(0.25)
        assert snapshot["running"] is True

    def test_complete_phase_clears_running(self):
        from app.services.data_downloader import _publish, _progress, get_progress

        _progress.update({"symbols": ["A", "B"]})
        _publish(None, 2, 2, "complete")

        snapshot = get_progress()
        assert snapshot["running"] is False
        assert snapshot["progress"] == pytest.approx(1.0)

    def test_progress_is_published_on_the_bus(self):
        from app.services import data_downloader as dd
        from app.services.event_bus import EVENT_DATA_DOWNLOAD, bus

        queue = bus.subscribe()
        try:
            dd._progress.update({"symbols": ["A", "B"]})
            dd._publish("A", 1, 2, "symbol_complete")

            message = queue.get_nowait()
            assert message["event"] == EVENT_DATA_DOWNLOAD
            assert message["symbol"] == "A"
            assert message["progress"] == pytest.approx(0.5)
        finally:
            bus.unsubscribe(queue)
