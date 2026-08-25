/**
 * WebSocket client for the live event stream (§8, §9).
 *
 * Reconnects with exponential backoff. The connection state is exposed so
 * the UI can show that live data has stopped — a dashboard that silently
 * freezes on a dropped socket looks identical to a calm market, which is
 * exactly the confusion §1.7 exists to prevent.
 */

export type ConnectionState = "connecting" | "open" | "closed";

export interface BusEvent {
  event: string;
  timestamp?: string;
  [key: string]: unknown;
}

const WS_BASE = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000";

const INITIAL_RETRY_MS = 1000;
const MAX_RETRY_MS = 15000;

export interface WsClientOptions {
  onEvent: (event: BusEvent) => void;
  onStateChange?: (state: ConnectionState) => void;
}

export function createWsClient({ onEvent, onStateChange }: WsClientOptions) {
  let socket: WebSocket | null = null;
  let retryMs = INITIAL_RETRY_MS;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let closedByCaller = false;

  const setState = (state: ConnectionState) => onStateChange?.(state);

  const connect = () => {
    if (closedByCaller) return;

    setState("connecting");
    try {
      socket = new WebSocket(`${WS_BASE}/ws`);
    } catch {
      scheduleRetry();
      return;
    }

    socket.onopen = () => {
      retryMs = INITIAL_RETRY_MS;
      setState("open");
    };

    socket.onmessage = (message) => {
      try {
        const parsed = JSON.parse(message.data) as BusEvent;
        // Keep-alive frames are transport detail, not application events.
        if (parsed.event === "ping") return;
        onEvent(parsed);
      } catch {
        // A malformed frame must not tear down the connection.
      }
    };

    socket.onerror = () => socket?.close();

    socket.onclose = () => {
      setState("closed");
      scheduleRetry();
    };
  };

  const scheduleRetry = () => {
    if (closedByCaller || retryTimer) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
      connect();
    }, retryMs);
  };

  connect();

  return {
    close() {
      closedByCaller = true;
      if (retryTimer) clearTimeout(retryTimer);
      socket?.close();
      setState("closed");
    },
  };
}
