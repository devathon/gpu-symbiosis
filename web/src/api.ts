// Admin API client (spec 10). All endpoints are on the same origin
// (dev: Vite proxy; prod: served by the admin API).

export interface AppView {
  id: string;
  name: string;
  description: string | null;
  process_state: string;
  residency_state: string;
  state: string;
  pid: number | null;
  draining: boolean;
  active_requests: number;
  queued_requests: number;
  gpu_utilization_pct: number;
  gpu_memory_used_mb: number;
  gpu_memory_configured_mb: number;
  gpu_memory_effective_mb: number;
  startup_time_sec: number | null;
  load_time_sec: number | null;
  unload_time_sec: number | null;
  stop_time_sec: number | null;
  supports_load_unload: boolean;
  allow_automatic_unload: boolean;
  pinned: boolean;
  consecutive_load_failures: number;
  enabled: boolean;
  // form fields (spec 11 §2.3) — present for edit pre-fill
  process_name: string | null;
  application_type: string;
  start_script: string;
  stop_script: string | null;
  load_script: string | null;
  unload_script: string | null;
  port: number;
  protocol: string;
  health_check_url: string | null;
  ready_check_url: string | null;
  cpu_memory_required_mb: number | null;
  gpu_memory_safety_margin: number;
  idle_unload_timeout_sec: number | null;
  activity_detection_method: string;
  idle_threshold_gpu_pct: number;
}

export interface GwView {
  id: string;
  name: string;
  port: number;
  application_id: string;
  priority: number;
  max_waiting_queue: number;
  max_waiting_time_sec: number;
  wait_timeout_policy: string;
  request_timeout_sec: number | null;
  max_concurrent_requests: number;
  enabled: boolean;
  state: string;
  queue: { length: number; oldest_wait_sec: number };
  /** Per-gateway scheduler score + breakup (spec 06 §6.9); hover shows detail. */
  score?: ScoreBreakdown | null;
}

export interface Resources {
  total_mb: number;
  usable_mb: number;
  used_mb: number;
  available_mb: number;
  vram_pct: number;
  gpu_pct: number;
  temperature_c: number | null;
  clock_mhz: number | null;
  power_w: number | null;
  power_limit_w: number | null;
  name: string;
  pcie: string;
  rx_mbps: number | null;
  tx_mbps: number | null;
  ram_total_mb: number;
  ram_used_mb: number;
  cpu_pct: number;
  cpu_cores: number;
}

export interface Status {
  applications: Record<string, AppView>;
  gateways: Record<string, GwView>;
  resources: Resources;
}

export interface RequestRow {
  id: string;
  gateway_id: string;
  application_id: string;
  status: string;
  priority: number;
  received_at: string | null;
  queued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  queue_wait_sec: number | null;
  service_time_sec: number | null;
  error: string | null;
}

export interface EventRow {
  kind: "application" | "gateway" | "scheduler";
  event?: string;
  trigger?: string;
  decision?: string;
  candidate?: string | null;
  application_id?: string;
  gateway_id?: string;
  detail?: string | null;
  at: string;
}

export interface GpuSample {
  total_mb: number;
  used_mb: number;
  free_mb: number;
  available_mb: number;
  vram_pct: number;
  gpu_pct: number;
  temperature_c: number | null;
  clock_mhz: number | null;
  power_w: number | null;
  power_limit_w: number | null;
  name: string;
  pcie: string;
  rx_mbps: number | null;
  tx_mbps: number | null;
  ram_total_mb: number;
  ram_used_mb: number;
  cpu_pct: number;
  cpu_cores: number;
  reservations: Record<string, number>;
}

export interface ScoreBreakdown {
  score: number;
  backlog_pressure?: number;
  priority_pressure?: number;
  waiting_pressure?: number;
  arrival_rate?: number;
  service_rate?: number;
  demand_ratio?: number;
  normalized_demand?: number;
  recent_demand_pressure?: number;
}

export interface Decision {
  trigger?: string;
  candidate_gateway_id?: string;
  candidate_application_id?: string;
  candidate_score?: number;
  switching_penalty?: number;
  effective_score?: number;
  current_score?: number;
  decision?: string;
  detail?: string;
  at?: string | number;
  active_application_id?: string;
  scores?: ScoreBreakdown;
  guards?: { min_residency_left?: number; cooldown_left?: number; emergency?: boolean };
  residents?: string[];
  vram?: {
    total_mb: number;
    reserve_mb: number;
    usable_mb: number;
    resident_mb: number;
    reserved_mb: number;
    external_mb: number;
    free_mb: number;
  };
  switch_tracking?: {
    switch_count: number;
    last_switch_at: number | null;
    last_pair: [string, string] | null;
    previous_application: string | null;
  };
  boosts?: Record<string, number>;
  config?: {
    min_residency_sec: number;
    switch_margin: number;
    switch_cost_weight: number;
    emergency_wait_sec: number | null;
    emergency_queue_threshold: number | null;
    switch_cooldown_sec: number;
    process_now_multiplier: number;
    process_now_boost_ttl_sec: number;
  };
}

export interface MetricItem {
  id: string;
  name: string;
  unit: string;
  kind: string;
  display: string;
  group?: string;
  value: number | string | null;
}

export interface AppMetrics {
  application_id: string;
  type: string;
  window: string;
  state: {
    process_state: string;
    residency_state: string;
    active_requests: number;
    queued_requests: number;
  };
  metrics: MetricItem[];
  raw: Record<string, unknown>;
  active_time: {
    total_active_time?: number;
    loaded_time?: number;
    processing_time?: number;
    idle_loaded_time?: number;
  };
  spec: MetricItem[];
}

// -- queue inspector (spec 11 §4) ----------------------------------------------
export interface QueueRow {
  request_id: string;
  priority: number;
  queued_at: number;
  waiting_sec: number;
  received_at: string | null;
  metadata: Record<string, unknown>;
}

export interface KeyRow {
  id: string;
  name: string | null;
  created_at: string | null;
}

// -- application form (spec 11 §2.3) ------------------------------------------
export interface AppFormFields {
  id: string;
  name: string;
  description: string;
  application_type: string;
  process_name: string;
  start_script: string;
  stop_script: string | null;
  load_script: string | null;
  unload_script: string | null;
  port: number;
  protocol: string;
  health_check_url: string | null;
  ready_check_url: string | null;
  configured_gpu_memory_mb: number;
  cpu_memory_required_mb: number | null;
  gpu_memory_safety_margin: number;
  supports_load_unload: boolean;
  allow_automatic_unload: boolean;
  idle_unload_timeout_sec: number | null;
  activity_detection_method: string;
  idle_threshold_gpu_pct: number;
}

// -- gateway form (spec 11 §3) --------------------------------------------------
export interface GatewayFormFields {
  name: string;
  port: number;
  application_id: string;
  priority: number;
  max_waiting_queue: number;
  max_waiting_time_sec: number;
  wait_timeout_policy: string;
  request_timeout_sec: number | null;
  max_concurrent_requests: number;
  enabled: boolean;
}

// -- auth (spec 12 §3) ----------------------------------------------------------
// Double-submit CSRF: the token travels in the X-CSRF-Token header on every
// mutating request. It is session-scoped and re-acquired after a page reload.
let _csrf: string | null = null;

export function csrfToken(): string | null {
  return _csrf;
}

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return (await r.json()) as T;
}

async function mut<T = void>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (_csrf) headers["X-CSRF-Token"] = _csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  try {
    return (await r.json()) as T;
  } catch {
    return undefined as T;
  }
}

export async function initAuth(): Promise<{ mode: "dev" | "authed" | "login"; username?: string }> {
  try {
    const me = await fetch("/api/auth/me");
    if (me.ok) {
      const r = (await me.json()) as { username: string };
      const c = (await (await fetch("/api/auth/csrf")).json()) as { csrf: string };
      _csrf = c.csrf;
      return { mode: "authed", username: r.username };
    }
  } catch {
    // fall through
  }
  let admins: { admins: string[] } = { admins: [] };
  try {
    admins = await (await fetch("/api/auth/admins")).json();
  } catch {
    // no admin endpoint reachable → treat as dev
  }
  if (admins.admins?.length > 0) return { mode: "login" };
  return { mode: "dev" };
}

export async function login(username: string, password: string): Promise<void> {
  const r = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  const c = (await r.json()) as { csrf: string };
  _csrf = c.csrf;
}

export async function logout(): Promise<void> {
  await mut("POST", "/api/auth/logout");
  _csrf = null;
}

export const api = {
  status: () => getJson<Status>("/api/status"),
  appTypes: () => getJson<{ types: string[] }>("/api/application-types"),
  appMetrics: (id: string, window = "1h") =>
    getJson<AppMetrics>(`/api/applications/${id}/metrics?window=${window}`),
  requestMetrics: () =>
    getJson<Record<string, Record<string, number>>>(`/api/metrics/request`),
  metricsSummary: () =>
    getJson<Record<string, Record<string, Record<string, number>>>>(`/api/metrics/summary`),
  metricsHistory: (scope: string, metric: string, window = "1m") =>
    getJson<{ at: string; min: number; max: number; avg: number; count: number }[]>(
      `/api/metrics/history?scope=${scope}&metric=${metric}&window=${window}`
    ),
  requests: (limit = 50) => getJson<RequestRow[]>(`/api/requests?limit=${limit}`),
  events: (limit = 100) => getJson<EventRow[]>(`/api/events?limit=${limit}`),
  gpu: () => getJson<GpuSample>("/api/gpu"),
  decision: () => getJson<Decision>("/api/scheduler/current-decision"),

  // -- application lifecycle -------------------------------------------------
  appStart: (id: string) => mut("POST", `/api/applications/${id}/start`),
  appStop: (id: string, force = false) =>
    mut("POST", `/api/applications/${id}/stop?force=${force}${force ? "&confirm=true" : ""}`),
  appLoad: (id: string) => mut("POST", `/api/applications/${id}/load`),
  appUnload: (id: string) => mut("POST", `/api/applications/${id}/unload`),
  appPin: (id: string) => mut("POST", `/api/applications/${id}/pin`),
  appUnpin: (id: string) => mut("POST", `/api/applications/${id}/unpin`),

  // -- application CRUD (spec 11 §2) ------------------------------------------
  appCreate: (fields: AppFormFields) => mut("POST", "/api/applications", fields),
  appUpdate: (id: string, fields: Partial<AppFormFields>) =>
    mut("PUT", `/api/applications/${id}`, fields),
  appDelete: (id: string) => mut("DELETE", `/api/applications/${id}?confirm=true`),

  // -- gateway lifecycle -------------------------------------------------------
  gwEnable: (id: string) => mut("POST", `/api/gateways/${id}/enable`),
  gwDisable: (id: string) => mut("POST", `/api/gateways/${id}/disable`),
  gwClear: (id: string) => mut("POST", `/api/gateways/${id}/clear-queue?confirm=true`),
  gwProcessNow: (id: string) => mut("POST", `/api/gateways/${id}/process-now`),

  // -- gateway CRUD (spec 11 §3) -----------------------------------------------
  gwCreate: (fields: GatewayFormFields) => mut("POST", "/api/gateways", fields),
  gwUpdate: (id: string, fields: Partial<GatewayFormFields>) =>
    mut("PUT", `/api/gateways/${id}`, fields),
  gwDelete: (id: string) => mut("DELETE", `/api/gateways/${id}?confirm=true`),

  // -- per-gateway API keys (spec 12 §2) ----------------------------------------
  gwKeys: (id: string) => getJson<{ keys: KeyRow[] }>(`/api/gateways/${id}/keys`),
  gwKeyCreate: (id: string, name: string) =>
    mut<{ key: string }>("POST", `/api/gateways/${id}/keys?name=${encodeURIComponent(name)}`),
  gwKeyDelete: (id: string, keyId: string) =>
    mut("DELETE", `/api/gateways/${id}/keys/${keyId}`),

  // -- queue request inspector (spec 11 §4) -------------------------------------
  gwQueue: (id: string) => getJson<{ gateway_id: string; queue: QueueRow[] }>(`/api/gateways/${id}/queue`),
  gwQueueForce: (id: string, requestId: string) =>
    mut("POST", `/api/gateways/${id}/queue/${requestId}/force`),
  reqCancel: (id: string) => mut("POST", `/api/requests/${id}/cancel`),
};
