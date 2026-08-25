# Master Spec — Automated Crypto Trading Pipeline

**Status:** Planning document — build target: Google Antigravity (Gemini)
**Owner:** [you]
**Purpose:** Self-contained build specification. An AI coding agent (or human dev) should be able to build this system from this document alone, without needing to ask clarifying questions for anything covered here. Anything genuinely left open is marked explicitly in §11.

---

## 1. Core Principles (non-negotiable)

1. **No hardcoded values.** Coins traded, thresholds, retrain schedule, trade frequency — all config-driven (`.env` for infra secrets, DB `config` table for everything else). Never baked into code.
2. **Config once, control from UI after.** Secrets go in `.env` at setup time, once. Everything operational (which models run, start/stop, manual retrain, emergency stop, stage switching) is controlled from the dashboard.
3. **No data loss on machine migration.** Code lives in GitHub. Data (models, trades, account state) lives in PostgreSQL, backed up via `pg_dump`, restorable on a new machine in one command.
4. **Paper before real.** Stage 3 (live money) is only reachable after Stage 2 (paper trading) produces a real-world performance record meeting the promotion gate (§5.4).
5. **Risk engine is independent of the prediction model.** It can veto or resize any trade regardless of what the model/LLM says.
6. **Realistic goal:** the model outputs a probability/confidence score, not a price prediction. Success = good risk-adjusted performance over time, not forecast accuracy.
7. **Fail safe, not silent.** Any component failure (data feed down, model missing, API error) should stop new trades and surface loudly on the dashboard — never trade on stale or missing data without flagging it.

---

## 2. Repository & Folder Structure

```
trading-pipeline/
├── .env                        # NOT committed — see .gitignore
├── .env.example                # committed, no real values
├── .gitignore
├── docker-compose.yml          # postgres + backend + frontend (optional but recommended)
├── README.md
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI entrypoint
│   │   ├── config.py           # loads .env, exposes settings object
│   │   ├── db/
│   │   │   ├── models.py       # SQLAlchemy models (see §3 schema)
│   │   │   ├── session.py
│   │   │   └── migrations/     # Alembic migrations
│   │   ├── api/
│   │   │   ├── routes_system.py    # start/stop, status, emergency stop
│   │   │   ├── routes_data.py      # data download endpoints
│   │   │   ├── routes_models.py    # model registry, train, promote
│   │   │   ├── routes_trades.py    # trade history, positions
│   │   │   ├── routes_config.py    # settings CRUD (coins, thresholds, PIN)
│   │   │   └── routes_ws.py        # WebSocket endpoint
│   │   ├── services/
│   │   │   ├── binance_client.py   # wraps python-binance, testnet/live switch
│   │   │   ├── data_downloader.py
│   │   │   ├── training_pipeline.py
│   │   │   ├── model_registry.py
│   │   │   ├── risk_engine.py
│   │   │   ├── llm_advisor.py      # Ollama/Gemini wrapper
│   │   │   ├── trading_engine.py   # order placement logic, both paper & live
│   │   │   └── scheduler.py        # APScheduler jobs (retrain, LLM check, trade loop)
│   │   └── models_store/           # trained model binary files (.pkl/.pt), gitignored
│   ├── requirements.txt
│   └── tests/
├── frontend/
│   ├── package.json
│   ├── app/                    # Next.js app router
│   │   ├── page.tsx             # Main dashboard
│   │   ├── models/page.tsx      # Models page
│   │   ├── settings/page.tsx    # Config + PIN management
│   │   └── components/
│   └── lib/ws-client.ts
└── scripts/
    ├── backup_db.sh             # pg_dump wrapper
    ├── restore_db.sh
    └── migrate_to_new_machine.md
```

---

## 3. Database Schema (PostgreSQL)

```sql
-- Candle/price data
CREATE TABLE candles (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,          -- e.g. 'BTCUSDT'
    interval VARCHAR(10) NOT NULL,        -- e.g. '4h'
    open_time TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    UNIQUE(symbol, interval, open_time)
);

-- Model registry
CREATE TABLE models (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol VARCHAR(20) NOT NULL,
    model_type VARCHAR(50) NOT NULL,      -- e.g. 'xgboost_classifier'
    file_path TEXT NOT NULL,              -- path in models_store/
    trained_at TIMESTAMPTZ NOT NULL,
    training_data_range TSTZRANGE,
    metrics JSONB NOT NULL,               -- {accuracy, precision, recall, sharpe, max_drawdown, ...}
    status VARCHAR(20) NOT NULL DEFAULT 'candidate',  -- candidate | active | archived
    notes TEXT
);

-- Trades (paper AND live — distinguished by `stage`)
CREATE TABLE trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stage VARCHAR(10) NOT NULL,           -- paper | live
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(4) NOT NULL,             -- buy | sell
    order_type VARCHAR(10) NOT NULL,      -- market | limit
    quantity NUMERIC NOT NULL,
    price NUMERIC,                        -- fill price
    model_id UUID REFERENCES models(id),
    model_confidence NUMERIC,
    risk_decision VARCHAR(10) NOT NULL,   -- approved | resized | rejected
    risk_notes JSONB,
    llm_context JSONB,                    -- snapshot of last LLM advisory at trade time
    status VARCHAR(15) NOT NULL,          -- filled | partial | cancelled | failed
    binance_order_id VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Wallet/account state snapshots (for paper AND live)
CREATE TABLE wallet_snapshots (
    id BIGSERIAL PRIMARY KEY,
    stage VARCHAR(10) NOT NULL,
    balances JSONB NOT NULL,              -- {"USDT": 1000, "BTC": 0.01, ...}
    total_value_usdt NUMERIC NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Risk engine decision log (every trade attempt, even rejected ones)
CREATE TABLE risk_log (
    id BIGSERIAL PRIMARY KEY,
    trade_id UUID REFERENCES trades(id),
    checks JSONB NOT NULL,                -- {"position_size_ok": true, "daily_loss_ok": false, ...}
    decision VARCHAR(10) NOT NULL,        -- approved | resized | rejected
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- LLM advisory log (max 2 entries/day by design)
CREATE TABLE llm_advisories (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(20) NOT NULL,        -- ollama | gemini
    prompt TEXT NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- System-wide config (replaces hardcoded values)
CREATE TABLE config (
    key VARCHAR(100) PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Seed rows: symbols, retrain_interval_hours, max_trades_per_day,
-- max_position_pct, max_daily_loss_pct, current_stage, stage_pin_hash,
-- llm_calls_per_day, promotion_gate (json: min_days, min_winrate, max_drawdown)

-- System status/heartbeat (drives online/offline indicators)
CREATE TABLE component_status (
    component VARCHAR(50) PRIMARY KEY,    -- 'data_feed','binance_api','risk_engine','llm_advisor','scheduler'
    status VARCHAR(10) NOT NULL,          -- online | offline | error
    last_heartbeat TIMESTAMPTZ NOT NULL,
    detail TEXT
);
```

---

## 4. Tech Stack & Key Libraries

| Layer | Choice | Notes |
|---|---|---|
| Backend | FastAPI (Python 3.11+) | async endpoints, WebSocket support built in |
| ORM | SQLAlchemy 2.0 + Alembic | Alembic for schema migrations, versioned in Git |
| Database | PostgreSQL 15+ | |
| Frontend | Next.js 14 (App Router) + React 18 | |
| Realtime | native WebSocket (FastAPI) ↔ `lib/ws-client.ts` on frontend | |
| Scheduler | APScheduler (in-process) | jobs: retrain, LLM check, trade loop, heartbeat |
| Exchange SDK | `python-binance` | supports testnet via base URL swap |
| ML | scikit-learn / XGBoost to start; PyTorch only if a baseline proves insufficient | keep Stage 1 baseline simple — see §5.1 |
| LLM | Ollama (local, default) via `ollama` Python client; Gemini via `google-generativeai` as fallback | |
| Containerization | Docker Compose (Postgres + backend + frontend as 3 services) | optional per your earlier answer, but strongly recommended for the "new laptop" migration case |

---

## 5. Stage Details

### 5.1 Stage 1 — Setup, Data, Training, Model Registry

**Data downloader (`services/data_downloader.py`):**
- Input: symbol list from `config` table (default `["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"]`), interval from config (default `4h`)
- Pulls historical klines via Binance REST (`/api/v3/klines`), paginated backward until a configurable history length is reached (default: 2 years)
- Upserts into `candles` table (unique constraint prevents duplicates)
- Triggered by: dashboard "Download Data" button, AND a scheduled daily job to keep data current
- Emits WebSocket progress events: `{"event":"data_download","progress":0.42,"symbol":"BTCUSDT"}`

**Baseline model (first one to build — keep simple):**
- Features: standard technical indicators computed from candles — SMA/EMA (20/50/100), RSI(14), MACD, Bollinger Band width, volume delta
- Target: binary classification — did price move up >X% in the next N candles (X and N config-driven, default X=1%, N=1 candle at 4h = next 4h)
- Model: `XGBoostClassifier` (fast to train, interpretable feature importance, good baseline before anything fancier)
- Output at inference: probability (0-1), used as "confidence" downstream — NOT a price prediction
- **Do not build multiple model types until this baseline runs end-to-end in Stage 2 successfully.**

**Training pipeline (`services/training_pipeline.py`):**
- Triggered by: scheduler (default every 12h, config-driven) OR manual "Train Now" button per symbol
- Steps: load candles for symbol → compute features → train/test split (time-based split, NOT random — never shuffle time series) → train → evaluate on holdout → write metrics + save model file to `models_store/` → insert row in `models` table with status `candidate`
- Never overwrites a previous model file — always a new UUID + file, old ones remain queryable/rollback-able

**Model registry & scoring (`services/model_registry.py`):**
- Scoring function combines: holdout accuracy, precision on the "up" class (false positives cost real money), and (once available from paper trading) realized win rate / drawdown
- Highest-scoring `candidate` model for a symbol gets promoted to `active`; previous `active` model moves to `archived` (not deleted)
- Manual override: dashboard lets user force-promote/demote any model regardless of score

**LLM advisor (`services/llm_advisor.py`):**
- Called on a schedule: default 2x/day (config-driven `llm_calls_per_day`), e.g. 00:00 and 12:00 UTC
- Prompt template asks for: macro/world-economy context relevant to crypto markets, and a qualitative view on the current active models' target symbols
- Response stored in `llm_advisories` table (full JSON, timestamped)
- Used as **context only** — added to `trades.llm_context` at trade time for audit, and can inform risk engine's confidence threshold (e.g. risk engine can require higher model confidence if latest LLM advisory flags high macro uncertainty) — but the LLM never directly triggers a trade

### 5.2 Stage 2 — Paper Trading

- Uses **Binance Testnet** (real order-matching engine, fake funds) rather than a pure simulation — gives realistic fill behavior/slippage
- Same `trading_engine.py` code path as live — the only difference is `BINANCE_TESTNET=true` at the client level, controlled by `current_stage` in config, not a code fork
- Every trade goes through risk engine exactly as live would
- Dashboard shows: running win rate, P&L in USDT, max drawdown, trade count — updated live via WebSocket after every fill

### 5.3 Stage 3 — Live Trading

- `current_stage` switched to `live` via dashboard, gated by PIN (see §7 Security)
- Identical code path, `BINANCE_TESTNET=false`
- Risk engine checks become **stricter defaults** when stage=live (e.g. tighter position sizing) — configurable but the UI should visibly warn if live thresholds are looser than paper thresholds were
- Emergency stop fully armed (see §6)

### 5.4 Stage 2 → 3 Promotion Gate

Stored in `config` as `promotion_gate` JSON, default suggested values (adjust as you like before Stage 3 is reachable):
```json
{
  "min_paper_trading_days": 30,
  "min_trade_count": 40,
  "min_win_rate": 0.52,
  "max_drawdown_pct": 15
}
```
Dashboard computes current paper-trading stats against this gate and **disables the switch-to-live control** (greyed out, not just warned) until all four conditions are met. This is a hard gate, not a suggestion — the button should be genuinely unclickable until met.

---

## 6. Risk Engine — Detailed Rules

Runs synchronously before every order attempt, in both paper and live stages. Each check returns pass/fail + detail; engine emits `approved`, `resized`, or `rejected` with full reasoning stored in `risk_log`.

| Check | Rule (config-driven default) | On fail |
|---|---|---|
| Position sizing | Single trade ≤ `max_position_pct` of total wallet value (default 10%) | Resize down to limit, or reject if resized size < minimum viable order |
| Daily loss cap | If realized P&L today ≤ `-max_daily_loss_pct` (default -5%) | Reject all new trades until next UTC day |
| Model confidence floor | Model output probability must exceed `min_confidence` (default 0.6) | Reject |
| Volatility/liquidity sanity | Recent candle range and volume must be within historical normal bounds (e.g. not >3 std dev spike, indicating a possible data glitch or flash event) | Reject, flag for manual review |
| Correlation/concentration | Total exposure across all symbols ≤ `max_total_exposure_pct` (default 80%, leaving cash buffer) | Reject or resize |
| Component health | `binance_api`, `data_feed` in `component_status` must be `online` and heartbeat within last 5 min | Reject, surface alert on dashboard |

Risk engine logic should be pure/deterministic rules first (a rules-based engine, per your earlier note that ML is optional here) — an ML-based risk scorer can be added later as an additional check, not a replacement for these hard rules.

---

## 7. Emergency Stop — Exact Behavior

Triggered by the always-visible dashboard button. On trigger:
1. Immediately set `current_stage` effective trading flag to `halted` (distinct from `setup/paper/live` — halted overrides whichever stage was active)
2. Cancel all open orders via Binance API
3. **Do NOT auto-liquidate existing holdings** (default behavior) — freeze only
4. Stop the scheduler's trade-loop job (training jobs may continue — training is not a financial risk)
5. Surface a persistent, unmissable banner on the dashboard: "TRADING HALTED — Emergency stop active" with a timestamp and manual "Resume" button (resume requires re-entering the stage PIN)
6. A **separate, explicitly distinct button** — "Liquidate all to USDT" — exists for the case where the user does want to exit positions, requiring a second confirmation step ("Are you sure? This will sell all holdings at market price.")

---

## 8. Dashboard — Detailed Page Specs

### 8.1 Main Page (`/`)
- **Header:** current stage badge (Setup/Paper/Live/Halted), overall system online/offline dot
- **Component status strip:** one indicator each for Data Feed, Binance API, Risk Engine, LLM Advisor, Scheduler — green/red/yellow dot + last heartbeat time (from `component_status` table, polled or pushed via WebSocket)
- **Control buttons:** Start System, Download Data, Train Now, Emergency Stop (red, top-right, always visible regardless of scroll)
- **Wallet panel:** current balances per asset, total value in USDT, small sparkline of value over time (from `wallet_snapshots`)
- **Open positions table:** symbol, quantity, entry price, current price, unrealized P&L
- **Recent trades feed:** live-updating list (WebSocket) — symbol, side, price, model used, risk decision, timestamp
- **Stage progress widget (paper mode only):** shows current stats vs. promotion gate (§5.4) as a checklist with progress bars

### 8.2 Models Page (`/models`)
- Table of all models: symbol, type, trained_at, status badge, key metrics (accuracy, win rate if available), file size
- Per-row actions: "View Details" (metrics breakdown, feature importance chart), "Promote to Active", "Retrain Now", "Archive"
- Top-of-page: "Train All" button, filter by symbol/status

### 8.3 Settings Page (`/settings`)
- Symbol list editor (add/remove traded coins)
- Retrain interval, trade frequency cap, risk thresholds (all fields from `config` table) — form-based, writes to DB, no restart needed
- Stage PIN management (change PIN, requires entering current PIN first)
- Promotion gate thresholds editor

All three pages share a WebSocket connection (`lib/ws-client.ts`) subscribed to: `trade_event`, `training_progress`, `component_status_change`, `data_download_progress`.

---

## 9. API Endpoints (FastAPI, indicative)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/system/start` | Start full pipeline per current config |
| POST | `/api/system/emergency-stop` | Trigger halt (§7) |
| POST | `/api/system/resume` | Resume after halt, requires PIN |
| POST | `/api/system/liquidate` | Sell all to USDT, requires confirmation flag |
| GET | `/api/status` | Component statuses, current stage |
| POST | `/api/data/download` | Trigger data download job |
| GET | `/api/data/download/progress` | Poll fallback if WebSocket unavailable |
| GET | `/api/models` | List models |
| POST | `/api/models/{symbol}/train` | Manual retrain |
| POST | `/api/models/{id}/promote` | Promote to active |
| POST | `/api/models/{id}/archive` | Archive |
| GET | `/api/trades` | Trade history, filterable by stage/symbol |
| GET | `/api/wallet` | Current balances + snapshots |
| GET | `/api/config` | Get all config |
| PUT | `/api/config/{key}` | Update one config value |
| POST | `/api/stage/switch` | Switch stage, requires PIN, enforces promotion gate for live |
| WS | `/ws` | Live event stream |

---

## 10. Security Notes (final)
- `.env` never committed — `.gitignore` from commit #1
- Binance API key: withdrawal permission **disabled**
- Dashboard assumed **localhost-only**; no session auth layer since `APP_SECRET_KEY` was removed per your decision — revisit if ever exposed beyond localhost
- Stage-switch PIN (default `000000`, changeable in Settings) gates paper→live only; UI must warn if PIN is still default when attempting to reach live stage
- DB backups encrypted if stored anywhere off the local machine

---

## 11. Genuinely Open Decisions (need your input, not inferable from prior conversation)

| Decision | Default used in this spec | Change if you want |
|---|---|---|
| History length for initial data download | 2 years | — |
| Baseline model target definition | >1% move in next 4h candle | — |
| Promotion gate numbers (§5.4) | 30 days / 40 trades / 52% win rate / 15% max drawdown | — |
| Risk engine thresholds (§6) | 10% max position, 5% max daily loss, 0.6 min confidence, 80% max exposure | — |
| Docker vs. bare-metal install | Docker Compose recommended | Bare install also fully compatible with this spec |

---

## 12. Build Order

1. Repo scaffold (`docker-compose.yml`, `.gitignore`, folder structure per §2)
2. DB schema + Alembic migration (§3)
3. `binance_client.py` (testnet-aware wrapper) + `data_downloader.py`, prove data lands in `candles`
4. Baseline model: feature engineering + `training_pipeline.py` for ONE symbol (BTCUSDT) end-to-end
5. Model registry + scoring (§5.1) — prove promotion logic works
6. Risk engine (§6) — rules-based, unit-testable independent of trading loop
7. `trading_engine.py` wired to Testnet, paper stage loop running on schedule
8. Dashboard: Main page skeleton + WebSocket wiring, then Models page, then Settings page
9. LLM advisor integration (2x/day scheduled job)
10. Promotion gate logic + stage-switch endpoint with PIN
11. Emergency stop + liquidate endpoints
12. Backup/restore scripts (`scripts/backup_db.sh`, `restore_db.sh`) + migration doc, tested by actually restoring on a second machine/VM before calling this "done"
13. Extend to remaining symbols (ETH/BNB/SOL) only after step 7-8 proven stable on BTC alone
