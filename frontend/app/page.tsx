"use client";

/**
 * Main dashboard (§8.1).
 *
 * Data arrives two ways: a polled snapshot for whole-state correctness, and
 * WebSocket events that trigger an immediate refresh of the affected panel.
 * Polling is the floor, not the mechanism — if the socket drops, the page
 * keeps showing true (if slightly older) data instead of silently freezing,
 * and the header says the live feed is down. The same rule applies to
 * navigating away and back: refreshAll() runs on every mount, so a download
 * or training run already in progress renders correctly immediately,
 * before any WebSocket event arrives — not just while the tab stayed open.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import AdvisoryPanel from "@/app/components/AdvisoryPanel";
import GateProgress from "@/app/components/GateProgress";
import HaltBanner from "@/app/components/HaltBanner";
import PerformancePanel from "@/app/components/PerformancePanel";
import LiquidatePanel from "@/app/components/LiquidatePanel";
import NavBar from "@/app/components/NavBar";
import PositionsTable from "@/app/components/PositionsTable";
import ReadinessTable from "@/app/components/ReadinessTable";
import StatusStrip from "@/app/components/StatusStrip";
import TradesFeed from "@/app/components/TradesFeed";
import WalletPanel from "@/app/components/WalletPanel";
import {
  downloadData,
  emergencyStop,
  generateAdvisory,
  getAdvisories,
  getGate,
  getPerformance,
  getPositions,
  getStatus,
  getTrades,
  getWallet,
  startSystem,
  stopSystem,
  testBinanceConnection,
  trainAll,
  type AdvisoriesResponse,
  type GateStatus,
  type Performance,
  type Position,
  type SystemStatus,
  type Trade,
  type Wallet,
} from "@/lib/api";
import { createWsClient, type ConnectionState } from "@/lib/ws-client";

const POLL_INTERVAL_MS = 10000;
// While a download or training run is in flight, poll faster so progress
// (and the button re-enabling) reflects backend-confirmed completion
// promptly instead of waiting out the idle interval.
const ACTIVE_POLL_INTERVAL_MS = 2000;

const STAGE_STYLES: Record<string, string> = {
  setup: "bg-zinc-500",
  paper: "bg-blue-600",
  live: "bg-emerald-600",
  halted: "bg-red-600",
};

export default function Dashboard() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [tradesError, setTradesError] = useState<string | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [positionsError, setPositionsError] = useState<string | null>(null);
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [walletError, setWalletError] = useState<string | null>(null);
  const [advisories, setAdvisories] = useState<AdvisoriesResponse | null>(null);
  const [advisoriesError, setAdvisoriesError] = useState<string | null>(null);
  const [performance, setPerformance] = useState<Performance | null>(null);
  const [performanceError, setPerformanceError] = useState<string | null>(null);
  const [gate, setGate] = useState<GateStatus | null>(null);
  const [gateError, setGateError] = useState<string | null>(null);
  const [wsState, setWsState] = useState<ConnectionState>("connecting");
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // True only for the gap between clicking Download/Train and the first
  // status refresh landing — after that, the derived flags below (backed
  // by status.latest_download/latest_training) are the source of truth,
  // not a local flag.
  const [downloadTriggering, setDownloadTriggering] = useState(false);
  const [trainTriggering, setTrainTriggering] = useState(false);
  const [testingBinance, setTestingBinance] = useState(false);
  const [binanceTestResult, setBinanceTestResult] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    const result = await getStatus();
    if (result.ok) {
      setStatus(result.data);
      setStatusError(null);
    } else {
      setStatusError(result.error);
    }
  }, []);

  const refreshTrades = useCallback(async () => {
    const result = await getTrades();
    if (result.ok) {
      setTrades(result.data.trades);
      setTradesError(null);
    } else {
      setTradesError(result.error);
    }
  }, []);

  const refreshPositions = useCallback(async () => {
    const result = await getPositions();
    if (result.ok) {
      setPositions(result.data.positions);
      setPositionsError(null);
    } else {
      setPositionsError(result.error);
    }
  }, []);

  const refreshWallet = useCallback(async () => {
    const result = await getWallet();
    if (result.ok) {
      setWallet(result.data);
      setWalletError(null);
    } else {
      setWalletError(result.error);
    }
  }, []);

  const refreshAdvisories = useCallback(async () => {
    const result = await getAdvisories();
    if (result.ok) {
      setAdvisories(result.data);
      setAdvisoriesError(null);
    } else {
      setAdvisoriesError(result.error);
    }
  }, []);

  const refreshPerformance = useCallback(async () => {
    const result = await getPerformance();
    if (result.ok) {
      setPerformance(result.data);
      setPerformanceError(null);
    } else {
      setPerformanceError(result.error);
    }
  }, []);

  const refreshGate = useCallback(async () => {
    const result = await getGate();
    if (result.ok) {
      setGate(result.data);
      setGateError(null);
    } else {
      setGateError(result.error);
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([
      refreshStatus(),
      refreshTrades(),
      refreshPositions(),
      refreshWallet(),
      refreshAdvisories(),
      refreshPerformance(),
      refreshGate(),
    ]);
  }, [
    refreshStatus, refreshTrades, refreshPositions, refreshWallet,
    refreshAdvisories, refreshPerformance, refreshGate,
  ]);

  // Keep the latest refreshers reachable from the WebSocket callback without
  // tearing down and rebuilding the socket on every render.
  const handlers = useRef({
    refreshAll, refreshStatus, refreshTrades, refreshPositions, refreshWallet,
    refreshAdvisories, refreshPerformance, refreshGate,
  });
  handlers.current = {
    refreshAll, refreshStatus, refreshTrades, refreshPositions, refreshWallet,
    refreshAdvisories, refreshPerformance, refreshGate,
  };

  const downloading = downloadTriggering || status?.latest_download?.status === "running";
  const trainingRunning = trainTriggering || status?.latest_training?.status === "running";

  // Fetch-first, always: this runs before the WebSocket connects below, so
  // the page shows real backend state immediately on mount (including
  // right after navigating back from Models/Settings) rather than waiting
  // on the next live event. The poll then keeps that true even if the
  // socket drops, and speeds up while a download/training run is in
  // flight so a busy button clears promptly once the backend confirms
  // completion.
  useEffect(() => {
    void refreshAll();
    const intervalMs = downloading || trainingRunning ? ACTIVE_POLL_INTERVAL_MS : POLL_INTERVAL_MS;
    const timer = setInterval(() => void refreshAll(), intervalMs);
    return () => clearInterval(timer);
  }, [refreshAll, downloading, trainingRunning]);

  useEffect(() => {
    const client = createWsClient({
      onStateChange: setWsState,
      onEvent: (event) => {
        switch (event.event) {
          case "trade_event":
            void handlers.current.refreshTrades();
            void handlers.current.refreshPositions();
            void handlers.current.refreshPerformance();
            void handlers.current.refreshGate();
            break;
          case "data_download_progress": {
            // The event is a live nudge to refetch, never treated as state
            // by itself — /api/status (which carries latest_download) is
            // the source of truth, same pattern as component_status_change.
            const completed = event.completed ?? "?";
            const total = event.total ?? "?";
            setNotice(`Data download: ${completed}/${total} symbols`);
            setDownloadTriggering(false);
            void handlers.current.refreshStatus();
            break;
          }
          case "wallet_update":
            void handlers.current.refreshWallet();
            break;
          case "component_status_change":
            void handlers.current.refreshStatus();
            break;
          case "llm_advisory":
            void handlers.current.refreshAdvisories();
            break;
          case "training_progress": {
            const symbol = String(event.symbol ?? "");
            const phase = String(event.phase ?? "");
            setNotice(`Training ${symbol}: ${phase}`);
            setTrainTriggering(false);
            void handlers.current.refreshStatus();
            break;
          }
          case "system_event":
            setNotice(String(event.message ?? ""));
            void handlers.current.refreshStatus();
            break;
        }
      },
    });
    return () => client.close();
  }, []);

  const runAction = async (action: () => Promise<unknown>, label: string) => {
    setBusy(true);
    setNotice(`${label}…`);
    try {
      await action();
      await refreshAll();
      setNotice(`${label} done.`);
    } finally {
      setBusy(false);
    }
  };

  const handleDownload = async () => {
    setDownloadTriggering(true);
    setNotice("Data download…");
    try {
      const result = await downloadData();
      if (!result.ok) {
        setNotice(`Data download failed to start: ${result.error}`);
        setDownloadTriggering(false);
        return;
      }
      // The POST only confirms the job was *scheduled*, not finished — the
      // button stays disabled via `downloading` (status.latest_download)
      // until the backend reports a terminal status, not until this
      // resolves. This is the fix for a slow download re-enabling the
      // button early.
      await refreshStatus();
    } finally {
      setDownloadTriggering(false);
    }
  };

  const handleTrainAll = async () => {
    setTrainTriggering(true);
    setNotice("Training…");
    try {
      const result = await trainAll();
      if (!result.ok) {
        setNotice(`Training failed to start: ${result.error}`);
        setTrainTriggering(false);
        return;
      }
      await refreshStatus();
    } finally {
      setTrainTriggering(false);
    }
  };

  const handleTestBinance = async () => {
    setTestingBinance(true);
    setBinanceTestResult(null);
    try {
      const result = await testBinanceConnection();
      if (result.ok) {
        setBinanceTestResult(result.data.detail);
      } else {
        setBinanceTestResult(`Test failed: ${result.error}`);
      }
      await refreshStatus();
    } finally {
      setTestingBinance(false);
    }
  };

  const halted = status?.halted ?? false;
  const stage = status?.stage ?? "unknown";
  const download = status?.latest_download ?? null;
  const downloadDetail = download?.detail as { total?: number; completed?: unknown[] } | null;

  return (
    <div className="min-h-screen bg-zinc-50 p-4 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100 sm:p-6">
      <div className="mx-auto max-w-7xl space-y-4">
        {/* Header */}
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold">Trading Pipeline</h1>
            <NavBar />
            <span
              className={`rounded px-2 py-0.5 text-xs font-medium uppercase text-white ${
                STAGE_STYLES[stage] ?? "bg-zinc-500"
              }`}
            >
              {stage}
            </span>
            <span
              className="flex items-center gap-1.5 text-xs text-zinc-500"
              title={`WebSocket ${wsState}`}
            >
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  wsState === "open"
                    ? "bg-emerald-500"
                    : wsState === "connecting"
                      ? "bg-amber-500"
                      : "bg-red-500"
                }`}
              />
              live {wsState}
            </span>
          </div>

          {/* Emergency stop stays visible regardless of scroll (§8.1). */}
          <button
            onClick={() => void runAction(emergencyStop, "Emergency stop")}
            disabled={busy || halted}
            className="rounded-md bg-red-600 px-4 py-2 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50"
          >
            Emergency Stop
          </button>
        </header>

        {/* Halt banner with Resume (§7) */}
        {status && (
          <HaltBanner status={status} onResumed={() => void refreshAll()} />
        )}

        {/* Stuck orders (§1.7) */}
        {status && status.orders_needing_attention.length > 0 && (
          <div className="rounded-lg border border-amber-500 bg-amber-500/10 p-4">
            <p className="font-semibold text-amber-700 dark:text-amber-400">
              {status.orders_needing_attention.length} order(s) need attention
            </p>
            <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
              These could not be reconciled after the retry limit, so the
              position state may be wrong.
            </p>
            <ul className="mt-2 space-y-1 font-mono text-xs">
              {status.orders_needing_attention.map((order) => (
                <li key={order.trade_id}>
                  {order.trade_id.slice(0, 8)} · {order.side} {order.quantity}{" "}
                  {order.symbol} · {order.detail ?? "unknown error"}
                </li>
              ))}
            </ul>
          </div>
        )}

        {statusError && (
          <div className="rounded-lg border border-red-500 bg-red-500/10 p-4 text-sm">
            <span className="font-semibold text-red-600 dark:text-red-400">
              Backend unreachable:
            </span>{" "}
            <span className="text-zinc-600 dark:text-zinc-400">
              {statusError}
            </span>
          </div>
        )}

        {/* Controls (§8.1) */}
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => void runAction(startSystem, "Start system")}
            disabled={busy || halted || status?.trading_enabled}
            className="rounded-md bg-emerald-600 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            Start System
          </button>
          <button
            onClick={() => void runAction(stopSystem, "Stop system")}
            disabled={busy || !status?.trading_enabled}
            className="rounded-md bg-zinc-700 px-3 py-2 text-sm font-medium text-white hover:bg-zinc-800 disabled:opacity-50"
          >
            Stop
          </button>
          <button
            onClick={() => void handleTrainAll()}
            disabled={trainingRunning}
            className="rounded-md border border-zinc-300 px-3 py-2 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            {trainingRunning ? "Training…" : "Train Now"}
          </button>
          <button
            onClick={() => void runAction(generateAdvisory, "LLM advisory")}
            disabled={busy}
            className="rounded-md border border-zinc-300 px-3 py-2 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            Get Advisory
          </button>
          <button
            onClick={() => void handleDownload()}
            disabled={downloading}
            className="rounded-md border border-zinc-300 px-3 py-2 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            {downloading
              ? download?.progress != null
                ? `Downloading… ${Math.round(download.progress * 100)}%`
                : "Downloading…"
              : "Download Data"}
          </button>

          {status && !status.trading_allowed && (
            <span className="text-xs text-amber-600 dark:text-amber-500">
              {status.trading_blocked_reason}
            </span>
          )}
          {notice && (
            <span className="ml-auto text-xs text-zinc-500">{notice}</span>
          )}
        </div>

        {download?.status === "running" && (
          <div className="rounded-lg border border-blue-300 bg-blue-500/10 p-3 dark:border-blue-900">
            <div className="flex justify-between text-xs">
              <span>Downloading data{download.symbol ? ` — ${download.symbol}` : ""}</span>
              <span className="font-mono tabular-nums">
                {downloadDetail?.completed?.length ?? 0}/{downloadDetail?.total ?? "?"}
              </span>
            </div>
            <div className="mt-1 h-1.5 w-full rounded bg-zinc-200 dark:bg-zinc-800">
              <div
                className="h-1.5 rounded bg-blue-500 transition-all"
                style={{ width: `${(download.progress ?? 0) * 100}%` }}
              />
            </div>
          </div>
        )}

        {/* Component status strip (§8.1) */}
        {status && <StatusStrip components={status.components} />}

        {/* System readiness table (§8.1) */}
        {status && (
          <ReadinessTable
            components={status.components}
            latestDownload={status.latest_download}
            latestTraining={status.latest_training}
            stage={status.stage}
            halted={status.halted}
            tradingEnabled={status.trading_enabled}
            schedulerRunning={status.scheduler_running}
            ordersNeedingAttention={status.orders_needing_attention}
            onTestBinance={() => void handleTestBinance()}
            testingBinance={testingBinance}
            binanceTestResult={binanceTestResult}
          />
        )}

        {/* Panels */}
        <div className="grid gap-4 lg:grid-cols-3">
          <WalletPanel wallet={wallet} error={walletError} />
          <div className="lg:col-span-2">
            <PositionsTable positions={positions} error={positionsError} />
          </div>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <PerformancePanel data={performance} error={performanceError} />
          <GateProgress gate={gate} error={gateError} />
        </div>

        <div className="grid gap-4 lg:grid-cols-3">
          <AdvisoryPanel data={advisories} error={advisoriesError} />
          <div className="lg:col-span-2">
            <TradesFeed trades={trades} error={tradesError} />
          </div>
        </div>

        <LiquidatePanel onDone={() => void refreshAll()} />

        <footer className="pt-2 text-xs text-zinc-500">
          {status?.scheduler_running
            ? `Scheduler running · ${status.jobs.length} jobs · next trade loop ${
                status.jobs.find((j) => j.id === "trade_loop")?.next_run_at ??
                "—"
              }`
            : "Scheduler not running"}
        </footer>
      </div>
    </div>
  );
}
