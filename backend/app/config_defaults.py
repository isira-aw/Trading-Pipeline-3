"""Seed values for the DB-backed `config` table (§3, §5.4, §6, §11).

Core Principle #1: nothing operational is hardcoded in logic. These are the
*initial* values written to the `config` table on first migration; from then
on the table is authoritative and is edited from the Settings page.

Anything that reads one of these at runtime must go through
``app.services.config_service``, never import from here directly, or a
user's saved setting would be silently ignored.
"""

# Key -> (value, human-readable description shown in the Settings UI)
CONFIG_DEFAULTS: dict[str, object] = {
    # --- Data / universe (§5.1) ---
    "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"],
    "interval": "4h",
    "history_years": 2,

    # --- Training (§5.1, §11) ---
    "retrain_interval_hours": 12,
    # Target: did price move up more than X% within the next N candles.
    "target_move_pct": 1.0,
    "target_horizon_candles": 1,

    # --- Risk engine (§6, §11) ---
    "max_trades_per_day": 10,
    "max_position_pct": 10.0,
    "max_daily_loss_pct": 5.0,
    "min_confidence": 0.6,
    "max_total_exposure_pct": 80.0,
    # Volatility/liquidity sanity: reject candles beyond this many std devs.
    "volatility_sigma_limit": 3.0,
    # Component heartbeats older than this are treated as offline.
    "component_heartbeat_max_age_seconds": 300,
    # Orders below this notional are not worth placing.
    "min_order_notional_usdt": 10.0,
    # Components whose heartbeat must be healthy before any order. Never
    # includes risk_engine itself — see risk_engine.check_component_health.
    "required_healthy_components": ["binance_api", "data_feed"],
    # Candles used to establish "normal" range/volume for the sanity check.
    "volatility_lookback_candles": 100,
    # A day-open wallet baseline older than this cannot be trusted to
    # measure today's P&L (e.g. the bot was down for days).
    "max_pnl_baseline_age_hours": 24,

    # --- Scheduler (§4, §5.1) ---
    # How often the paper trade loop evaluates each symbol.
    "trade_loop_interval_minutes": 15,
    # How often component heartbeats are refreshed.
    "heartbeat_interval_seconds": 60,
    # How often unresolved orders are re-checked against the exchange.
    "reconcile_interval_minutes": 5,
    # Daily candle top-up, at this UTC hour.
    "data_refresh_hour_utc": 0,
    # Master switch for the trade loop — the Start/Stop control (§8.1).
    "trading_enabled": False,

    # --- Order reconciliation ---
    # Attempts before an unresolved order is escalated for manual attention.
    "reconcile_max_attempts": 6,
    # Exponential backoff between attempts, in seconds, capped.
    "reconcile_backoff_base_seconds": 30,
    "reconcile_backoff_max_seconds": 1800,

    # --- Stage control (§5.3, §7, §10) ---
    "current_stage": "setup",  # setup | paper | live | halted

    # --- LLM advisor (§5.1) ---
    "llm_calls_per_day": 2,
    "llm_provider": "ollama",  # ollama | gemini

    # --- Model registry scoring (§5.1) ---
    # Weights for the components of a candidate model's score. Precision on
    # the "up" class dominates because a false positive is a losing trade.
    "model_scoring_weights": {
        "precision_lift": 0.45,
        "discrimination": 0.25,
        "realized_win_rate": 0.30,
    },
    # A model that almost never fires gives too small a sample to trust its
    # precision, and would trade too rarely to be useful.
    "min_predicted_positive_rate": 0.02,
    # Realized trading results only outweigh holdout metrics once there are
    # enough closed trades to mean anything.
    "min_trades_for_realized_score": 20,

    # --- Promotion gate (§5.4) ---
    "promotion_gate": {
        "min_paper_trading_days": 30,
        "min_trade_count": 40,
        "min_win_rate": 0.52,
        "max_drawdown_pct": 15,
    },
}

# Components tracked in `component_status` (§3, §6 health check).
COMPONENTS = [
    "data_feed",
    "binance_api",
    "risk_engine",
    "llm_advisor",
    "scheduler",
    # Not a §8.1 component, but surfaces orders stuck after retry exhaustion
    # so they are visible rather than silently unresolved.
    "order_reconciliation",
]
