// Client for the txinTrade cloud, used by the remote web page only. The browser
// authenticates with the Worker's HttpOnly session cookie; no token is readable here.

// The dev server talks to `wrangler dev`; a build talks to production unless overridden.
export const CLOUD_ORIGIN = (import.meta.env.VITE_TXINTRADE_CLOUD_ORIGIN
  ?? (import.meta.env.DEV ? "http://localhost:8787" : "https://api.txintrade.com")).replace(/\/$/, "");

export class CloudError extends Error {
  code: string;
  status: number;

  constructor(code: string, status = 0) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

export type Profile = { email: string | null; display_name: string | null; picture_url: string | null };
export type Plan = "monthly";
/** The account's Creem subscription; null when it never subscribed. */
export type Billing = { plan: Plan | null; status: string; current_period_end: number | null; canceled_at: number | null };
export type Me = {
  account_id: string;
  profile: Profile | null;
  entitlements: Array<{ feature: string; status: string; expires_at: number | null }>;
  billing?: Billing | null;
};
export type Device = { id: string; label: string; created_at: number; revoked_at: number | null; online: boolean };

export type Command =
  | { operation: "status.read" }
  | { operation: "positions.list" }
  | { operation: "analyses.list"; limit: number }
  | { operation: "analyses.get"; analysis_id: string }
  | { operation: "analyses.start"; market_id: string; timeframe: string; output_locale: "zh-TW" | "en-US" };

type Job = { id: string; status: "dispatched" | "accepted" | "completed" | "failed" | "expired"; result?: unknown; error?: string };

async function cloud<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${CLOUD_ORIGIN}${path}`, { credentials: "include", ...init });
  } catch {
    throw new CloudError("network");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new CloudError(body?.error?.code ?? "service_unavailable", response.status);
  return body as T;
}

export const signInUrl = () =>
  `${CLOUD_ORIGIN}/auth/google/start?client=web&return_to=${encodeURIComponent(window.location.origin)}`;

export const readMe = () => cloud<Me>("/api/v1/me");

export const signOut = () => cloud<unknown>("/auth/logout", { method: "POST" });

/** Only the server grants remote access; the page just reads what it decided. */
export const hasRemoteAccess = (me: Me) => me.entitlements.some((item) => item.feature === "remote_access"
  && item.status === "active" && (item.expires_at === null || item.expires_at > Date.now()));

/** Go to Creem's checkout for a plan; Creem returns to this page with `?billing=success`. */
export async function startCheckout(plan: Plan): Promise<void> {
  const { checkout_url } = await cloud<{ checkout_url: string }>("/api/v1/billing/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan }),
  });
  window.location.assign(checkout_url);
}

/** Go to Creem's customer portal to cancel, change the card or see invoices. */
export async function openBillingPortal(): Promise<void> {
  const { url } = await cloud<{ url: string }>("/api/v1/billing/portal", { method: "POST" });
  window.location.assign(url);
}

export async function listDevices(): Promise<Device[]> {
  const { devices } = await cloud<{ devices: Device[] }>("/api/v1/devices");
  return devices.filter((device) => device.revoked_at === null);
}

const wait = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

/** Send one allowlisted command to a desktop and wait for its answer (the relay gives it 30 s). */
export async function runCommand<T>(deviceId: string, command: Command): Promise<T> {
  const created = await cloud<Job>(`/api/v1/devices/${deviceId}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(command),
  });
  let job = created;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (job.status === "completed") return job.result as T;
    if (job.status === "failed") throw new CloudError(job.error ?? "local_error");
    if (job.status === "expired") throw new CloudError("expired");
    await wait(Math.min(250 + attempt * 100, 1000));
    job = await cloud<Job>(`/api/v1/devices/${deviceId}/jobs/${created.id}`);
  }
  throw new CloudError("expired");
}

const RELAY_ERRORS = new Set([
  "device_offline", "device_timeout", "device_disconnected", "local_unavailable", "local_error",
  "unsupported_operation", "request_limit", "invalid_request", "subscription_required", "response_too_large",
]);

/**
 * Reach the computer's local API through the relay: same paths, methods and bodies
 * as the desktop app. Relay problems become `{detail}` errors the screens already show.
 */
export function relayTransport(
  deviceId: string,
  options: { describe: (code: string) => string; onSignedOut: () => void },
): (path: string, init?: RequestInit) => Promise<Response> {
  return async (path, init = {}) => {
    const url = new URL(path, "http://local");
    // Fund flows are public cloud data, the same for everyone: read them from the cloud
    // directly, so they show even while the computer is asleep or runs an older version.
    // Follow-up snapshots belong to the computer and go through the relay like the rest.
    const publicFlows = /^\/api\/v1\/smart-money\/(overview|(?:assets|reserves)(?:\/[A-Z0-9]{2,12})?)$/.exec(url.pathname);
    if (publicFlows && (init.method ?? "GET").toUpperCase() === "GET") {
      try {
        return await fetch(`${CLOUD_ORIGIN}/smart-money/${publicFlows[1]}${url.search}`, { signal: init.signal });
      } catch (reason) {
        if ((reason as Error).name === "AbortError") throw reason;
        return Response.json({ detail: options.describe("network") }, { status: 503 });
      }
    }
    const headers = new Headers(init.headers);
    const key = headers.get("Idempotency-Key");
    const request = {
      method: (init.method ?? "GET").toUpperCase(),
      path: url.pathname,
      query: url.search.slice(1),
      ...(typeof init.body === "string" ? { body: JSON.parse(init.body) } : {}),
      ...(key ? { idempotency_key: key } : {}),
    };
    let response: Response;
    try {
      response = await fetch(`${CLOUD_ORIGIN}/api/v1/devices/${deviceId}/requests`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
        signal: init.signal,
      });
    } catch (reason) {
      if ((reason as Error).name === "AbortError") throw reason;
      return Response.json({ detail: options.describe("network") }, { status: 503 });
    }
    if (response.ok) return response;
    const body = await response.clone().json().catch(() => null);
    const code = body?.error?.code;
    if (response.status === 401) options.onSignedOut();
    // Errors from the computer's own API pass through unchanged.
    if (typeof code !== "string") return response;
    return Response.json(
      { detail: options.describe(RELAY_ERRORS.has(code) || response.status === 401 ? code : "relay_error") },
      { status: response.status },
    );
  };
}

const STREAM_SILENCE_MS = 10_000;

type StreamListener = { onState: (data: string) => void; onError: () => void };
type StreamMessage = { type: "stream.event"; request_id: string; data: string } | { type: "stream.end"; request_id: string };

/**
 * This browser's own WebSocket to the relay, used for live follow-up replies.
 * It opens on first use with a single-use ticket and reconnects on the next use
 * after the relay ends it (at most every 15 minutes).
 */
export class RelaySocket {
  private socket: WebSocket | null = null;
  private ready: Promise<string> | null = null;
  private listeners = new Map<string, StreamListener>();
  // Events can arrive before the request that opened the stream has answered.
  private early = new Map<string, StreamMessage[]>();
  private ping = 0;

  private deviceId: string;

  constructor(deviceId: string) {
    this.deviceId = deviceId;
  }

  private connect(): Promise<string> {
    if (this.ready) return this.ready;
    this.ready = (async () => {
      const { websocket_path } = await cloud<{ websocket_path: string }>(
        `/api/v1/devices/${this.deviceId}/websocket-ticket`, { method: "POST" });
      const socket = new WebSocket(`${CLOUD_ORIGIN.replace(/^http/, "ws")}${websocket_path}`);
      this.socket = socket;
      return new Promise<string>((resolve, reject) => {
        socket.onmessage = (event) => {
          if (event.data === "pong") return;
          const message = JSON.parse(String(event.data));
          if (message.type === "relay.ready") {
            this.ping = window.setInterval(() => socket.readyState === WebSocket.OPEN && socket.send("ping"), 30_000);
            resolve(message.connection_id);
          } else if (message.type === "stream.event" || message.type === "stream.end") this.deliver(message);
        };
        socket.onclose = () => {
          reject(new CloudError("network"));
          this.closed(socket);
        };
      });
    })();
    this.ready.catch(() => { this.ready = null; });
    return this.ready;
  }

  private closed(socket: WebSocket) {
    if (this.socket !== socket) return;
    window.clearInterval(this.ping);
    this.socket = null;
    this.ready = null;
    // Open streams end; each falls back to reading the saved reply.
    for (const listener of this.listeners.values()) listener.onError();
    this.listeners.clear();
    this.early.clear();
  }

  private deliver(message: StreamMessage) {
    const listener = this.listeners.get(message.request_id);
    if (!listener) {
      const queued = this.early.get(message.request_id) ?? [];
      queued.push(message);
      this.early.set(message.request_id, queued);
      window.setTimeout(() => this.early.delete(message.request_id), 5_000);
      return;
    }
    if (message.type === "stream.end") {
      this.listeners.delete(message.request_id);
      listener.onError();
      return;
    }
    // The computer forwards each server-sent event as {event, data}.
    const event = JSON.parse(message.data) as { event: string; data: string };
    if (event.event === "state") listener.onState(event.data);
    else {
      this.listeners.delete(message.request_id);
      listener.onError();
    }
  }

  /** Stream a local event stream from the computer to this browser. */
  open(path: string, handlers: StreamListener): () => void {
    let requestId: string | null = null;
    let closed = false;
    const stop = () => {
      closed = true;
      window.clearTimeout(silence);
      if (requestId && this.listeners.delete(requestId)) this.cancel(requestId);
    };
    // A computer that never answers must not leave the reply waiting.
    const silence = window.setTimeout(() => {
      if (closed) return;
      stop();
      handlers.onError();
    }, STREAM_SILENCE_MS);
    const listener: StreamListener = {
      onState: (data) => { window.clearTimeout(silence); handlers.onState(data); },
      onError: () => { window.clearTimeout(silence); handlers.onError(); },
    };
    void (async () => {
      try {
        const connectionId = await this.connect();
        const response = await cloud<{ request_id: string }>(`/api/v1/devices/${this.deviceId}/requests`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ method: "GET", path, stream: true, connection_id: connectionId }),
        });
        requestId = response.request_id;
        if (closed) return this.cancel(requestId);
        this.listeners.set(requestId, listener);
        for (const message of this.early.get(requestId) ?? []) this.deliver(message);
        this.early.delete(requestId);
      } catch {
        if (!closed) listener.onError();
      }
    })();
    return stop;
  }

  private cancel(requestId: string) {
    if (this.socket?.readyState === WebSocket.OPEN)
      this.socket.send(JSON.stringify({ v: 1, type: "stream.cancel", request_id: requestId }));
  }

  close() {
    const socket = this.socket;
    if (socket) {
      this.closed(socket);
      socket.close(1000, "page_closed");
    }
  }
}
