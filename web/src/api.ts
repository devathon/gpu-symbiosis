// Admin API client (spec 10). All endpoints are on the same origin
// (dev: Vite proxy; prod: served by the admin API).

export interface AppView {
  id: string;
  name: string;
  process_state: string;
  residency_state: string;
  state: string;
  pid: number | null;
  draining: boolean;
  active_requests: number;
  queued_requests: number;
  gpu_memory_used_mb: number;
  gpu_memory_configured_mb: number;
  supports_load_unload: boolean;
  allow_automatic_unload: boolean;
  pinned: boolean;
  consecutive_load_failures: number;
  enabled: boolean;
}

export interface GwView {
  id: string;
  name: string;
  port: number;
  application_id: string;
  priority: number;
  wait_timeout_policy: string;
  max_concurrent_requests: number;
  enabled: boolean;
  state: string;
  queue: { length: number; oldest_wait_sec: number };
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

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return (await r.json()) as T;
}

async function post(url: string): Promise<void> {
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
}

export interface MetricItem {
  id: string;
  name: string;
  unit: string;
  kind: string;
  display: string;
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

export const api = {
  status: () => getJson<Status>("/api/status"),
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
  appStart: (id: string) => post(`/api/applications/${id}/start`),
  appStop: (id: string) => post(`/api/applications/${id}/stop?force=true`),
  appLoad: (id: string) => post(`/api/applications/${id}/load`),
  appUnload: (id: string) => post(`/api/applications/${id}/unload`),
  gwEnable: (id: string) => post(`/api/gateways/${id}/enable`),
  gwDisable: (id: string) => post(`/api/gateways/${id}/disable`),
  gwClear: (id: string) => post(`/api/gateways/${id}/clear-queue`),
  gwProcessNow: (id: string) => post(`/api/gateways/${id}/process-now`),
};
