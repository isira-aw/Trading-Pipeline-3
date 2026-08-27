/**
 * Typed access to the backend API (§9).
 *
 * Every call returns a discriminated result rather than throwing, because
 * a dead backend is a state the dashboard must *render* — greyed panels
 * and a visible error — not a state that blanks the page. §1.7: a failure
 * has to be loud, and a blank panel is not loud.
 */

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string };

// `undefined` return means the request was aborted (a newer poll for the
// same endpoint superseded it) — callers should leave existing state alone
// rather than treat it as an error.
export async function apiGet<T>(
  path: string,
  signal?: AbortSignal,
): Promise<ApiResult<T> | undefined> {
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      cache: "no-store",
      signal,
    });
    if (!response.ok) {
      return { ok: false, error: `${response.status} ${response.statusText}` };
    }
    return { ok: true, data: (await response.json()) as T };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      return undefined;
    }
    return {
      ok: false,
      error: error instanceof Error ? error.message : "Network error",
    };
  }
}

export async function apiPost<T>(
  path: string,
  body?: unknown,
): Promise<ApiResult<T>> {
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      return { ok: false, error: `${response.status} ${response.statusText}` };
    }
    return { ok: true, data: (await response.json()) as T };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "Network error",
    };
  }
}

export async function apiPut<T>(
  path: string,
  body?: unknown,
): Promise<ApiResult<T>> {
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      return {
        ok: false,
        error: detail?.detail ?? `${response.status} ${response.statusText}`,
      };
    }
    return { ok: true, data: (await response.json()) as T };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "Network error",
    };
  }
}

// ---------------------------------------------------------------------------
// Response shapes
// ---------------------------------------------------------------------------

export interface ComponentStatus {
  component: string;
  status: string;
  effective_status: string;
  last_heartbeat: string | null;
  age_seconds: number | null;
  detail: string | null;
}

export interface StuckOrder {
  trade_id: string;
  symbol: string;
  side: string;
  quantity: number;
  created_at: string;
  detail: string | null;
}

/** A row in `job_runs` — one audit-trail entry for a download or training
 * run, the DB-backed source of truth behind the readiness table (§8.1). */
export interface JobRun {
  job_id: string;
  job_type: "download" | "training";
  symbol: string | null;
  status: "running" | "success" | "failed";
  progress: number | null;
  detail: Record<string, unknown> | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SystemStatus {
  stage: string;
  halted: boolean;
  halted_at: string | null;
  halted_reason: string | null;
  trading_enabled: boolean;
  trading_allowed: boolean;
  trading_blocked_reason: string | null;
  components: ComponentStatus[];
  jobs: { id: string; next_run_at: string | null }[];
  scheduler_running: boolean;
  orders_needing_attention: StuckOrder[];
  websocket_clients: number;
  latest_download: JobRun | null;
  latest_training: JobRun | null;
  env_stale_warning: string | null;
}

export interface Trade {
  id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number | null;
  status: string;
  risk_decision: string;
  model_id: string | null;
  model_confidence: number | null;
  fee_usdt: number;
  stop_price: number | null;
  exit_reason: string | null;
  created_at: string;
  needs_attention: boolean;
}

export interface Position {
  symbol: string;
  quantity: number;
  entry_price: number;
  current_price: number | null;
  unrealized_pnl: number | null;
  unrealized_pnl_pct: number | null;
  opened_at: string;
  model_id: string | null;
  stop_price: number | null;
  stop_distance_pct: number | null;
}

export interface Wallet {
  stage: string;
  balances: Record<string, number>;
  total_value_usdt: number;
  live: boolean;
  live_error: string | null;
  history: { at: string; total_value_usdt: number }[];
}

export interface ModelRow {
  id: string;
  symbol: string;
  model_type: string;
  status: string;
  trained_at: string;
  notes: string | null;
  file_size_bytes: number | null;
  file_missing: boolean;
  metrics: Record<string, number | null>;
  feature_importance: Record<string, number>;
  score: number;
  score_breakdown: Record<string, number>;
  disqualified: boolean;
  disqualified_reason: string | null;
  used_realized_stats: boolean;
  realized: {
    closed_trades: number;
    win_rate: number | null;
    total_realized_pnl: number;
    max_drawdown_pct: number;
  };
}

export interface ModelsResponse {
  models: ModelRow[];
  scoring_weights: Record<string, number>;
  min_predicted_positive_rate: number;
  min_trades_for_realized_score: number;
}

export interface ConfigResponse {
  config: Record<string, unknown>;
  defaults: Record<string, unknown>;
  readonly_keys: string[];
  schedule_keys: string[];
  pin_is_default: boolean;
}

export const getModels = () => apiGet<ModelsResponse>("/api/models");
export const promoteModel = (id: string, force = false) =>
  apiPost(`/api/models/${id}/promote?force=${force}`);
export const archiveModel = (id: string) => apiPost(`/api/models/${id}/archive`);
export const promoteBest = (symbol: string) =>
  apiPost(`/api/models/${symbol}/promote-best`);
export const trainSymbol = (symbol: string) =>
  apiPost(`/api/models/train/${symbol}`);

export const getConfig = () => apiGet<ConfigResponse>("/api/config");
export const updateConfig = (key: string, value: unknown) =>
  apiPut(`/api/config/${key}`, { value });
export const changePin = (current_pin: string, new_pin: string) =>
  apiPost<{ changed: boolean }>("/api/config/pin", { current_pin, new_pin });

export interface Advisory {
  id: number;
  provider: string;
  created_at: string;
  status: string | null;
  uncertainty: string | null;
  uncertainty_reason: string | null;
  macro_summary: string | null;
  symbols: Record<string, { view?: string; comment?: string }> | null;
  key_risks: string[] | null;
  error: string | null;
}

export interface AdvisoriesResponse {
  advisories: Advisory[];
  calls_today: number;
  calls_rolling_24h: number;
  cap: number;
}

export const getAdvisories = (signal?: AbortSignal) =>
  apiGet<AdvisoriesResponse>("/api/advisories?limit=2", signal);
export const generateAdvisory = () =>
  apiPost<{ created: boolean; reason: string }>("/api/advisories/generate");

export interface GateCriterion {
  name: string;
  label: string;
  passed: boolean;
  current: number | null;
  required: number;
  comparison: string;
  detail: string;
}

export interface GateStatus {
  passed: boolean;
  criteria: GateCriterion[];
  stats: Record<string, number | null>;
  thresholds: Record<string, number>;
  summary: string;
  current_stage: string;
  can_switch_to_live: boolean;
  pin_is_default: boolean;
  pin_warning: { level: string; message: string } | null;
}

export const getGate = (signal?: AbortSignal) =>
  apiGet<GateStatus>("/api/stage/gate", signal);
export const switchStage = (stage: string, pin: string) =>
  apiPost<{ switched: boolean; to: string; pin_warning: { message: string } | null }>(
    "/api/stage/switch",
    { stage, pin },
  );

export interface Performance {
  closed_trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  total_realized_pnl: number;
  total_fees: number;
  max_drawdown_pct: number;
  open_lots: number;
  stage: string;
}

export const getPerformance = (signal?: AbortSignal) =>
  apiGet<Performance>("/api/performance", signal);
// Supersedes the old in-memory /api/data/download/progress poll fallback:
// this is DB-backed (job_runs), so it also survives a backend restart and
// answers correctly on first load, not just after a WS event.
export const getDownloadStatus = () => apiGet<JobRun | null>("/api/data/download/status");
export const getTrainingStatus = () => apiGet<JobRun | null>("/api/training/status");
export const testBinanceConnection = () =>
  apiPost<{ ok: boolean; detail: string }>("/api/system/test-binance");
export const trainAll = () => apiPost("/api/models/train-all");

export const getStatus = (signal?: AbortSignal) =>
  apiGet<SystemStatus>("/api/status", signal);
export const getTrades = (signal?: AbortSignal) =>
  apiGet<{ trades: Trade[] }>("/api/trades?limit=25", signal);
export const getPositions = (signal?: AbortSignal) =>
  apiGet<{ positions: Position[] }>("/api/positions", signal);
export const getWallet = (signal?: AbortSignal) =>
  apiGet<Wallet>("/api/wallet", signal);

export const startSystem = () => apiPost("/api/system/start");
export const stopSystem = () => apiPost("/api/system/stop");
export const emergencyStop = () => apiPost("/api/system/emergency-stop");
export const resumeSystem = (pin: string) =>
  apiPost<{ halted: boolean; stage: string }>("/api/system/resume", { pin });
export const liquidateAll = (pin: string, confirm: boolean) =>
  apiPost<{ placed_count: number; attempted_count: number; sales: unknown[] }>(
    "/api/system/liquidate",
    { pin, confirm },
  );
export const downloadData = () =>
  apiPost<{ status: string; job_id: string; symbols: string[]; message: string }>(
    "/api/data/download",
  );
