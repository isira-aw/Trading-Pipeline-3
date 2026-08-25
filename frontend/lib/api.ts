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

export async function apiGet<T>(path: string): Promise<ApiResult<T>> {
  try {
    const response = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
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

export interface SystemStatus {
  stage: string;
  trading_enabled: boolean;
  trading_allowed: boolean;
  trading_blocked_reason: string | null;
  components: ComponentStatus[];
  jobs: { id: string; next_run_at: string | null }[];
  scheduler_running: boolean;
  orders_needing_attention: StuckOrder[];
  websocket_clients: number;
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

export const getStatus = () => apiGet<SystemStatus>("/api/status");
export const getTrades = () => apiGet<{ trades: Trade[] }>("/api/trades?limit=25");
export const getPositions = () => apiGet<{ positions: Position[] }>("/api/positions");
export const getWallet = () => apiGet<Wallet>("/api/wallet");

export const startSystem = () => apiPost("/api/system/start");
export const stopSystem = () => apiPost("/api/system/stop");
export const emergencyStop = () => apiPost("/api/system/emergency-stop");
export const downloadData = () => apiPost("/api/data/download");
