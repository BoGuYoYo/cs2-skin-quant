/** 与 Worker 的 API 契约。注意后端统一返回 { ok, data } 或 { ok:false, error } */

const PW_KEY = 'skin-alpha-pw';

export function getPassword(): string {
  return localStorage.getItem(PW_KEY) ?? '';
}
export function setPassword(pw: string): void {
  if (pw) localStorage.setItem(PW_KEY, pw);
  else localStorage.removeItem(PW_KEY);
}

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? '';

type Params = Record<string, string | number | boolean | undefined | null>;

async function get<T>(path: string, params: Params = {}): Promise<T> {
  const url = new URL(BASE + path, window.location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
  }
  const pw = getPassword();
  const res = await fetch(url.toString(), {
    headers: pw ? { 'x-app-password': pw } : undefined,
  });
  let payload: { ok: boolean; data?: T; error?: string };
  try {
    payload = await res.json();
  } catch {
    throw new Error(`HTTP ${res.status} ${res.statusText}`);
  }
  if (!res.ok || !payload.ok) throw new Error(payload.error ?? `HTTP ${res.status}`);
  return payload.data as T;
}

// ------------------------------------------------------------------ 类型
export interface Meta {
  latestForecastDate: string | null;
  meta: Record<string, string>;
  counts: {
    items: number;
    cases: number;
    forecast_today: number;
    alerts_total: number;
  };
}

export interface Candidate {
  item_id: string;
  date?: string;
  name: string | null;
  market_hash_name?: string | null;
  image?: string | null;
  case_name?: string | null;
  wear?: string | null;
  price: number | null;
  dd_from_high: number | null;
  drop_duration_days: number | null;
  turnover: number | null;
  list_ratio: number | null;
  case_rarity_score: number | null;
  rel_ret_21d: number | null;
  q10: number | null;
  q50: number | null;
  q90: number | null;
  q10_adj: number | null;
  q90_adj: number | null;
  prob_up: number | null;
  score: number | null;
  side: string | null;
  size: number | null;
  why: string | null;
}

export interface CandidateResponse {
  date: string | null;
  rows: Candidate[];
}

export interface IndexRow {
  date: string;
  index_value: number | null;
  online_players: number | null;
  vol_turnover: number | null;
}

export interface KlineRow {
  date: string;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number | null;
}

export interface AlertRow {
  id: number;
  item_id: string;
  name: string | null;
  level: string;
  score: number | null;
  ts: number;
  detail: string | null;
  pushed?: number | null;
  image?: string | null;
  case_name?: string | null;
}

export interface CaseRow {
  case_id: string;
  case_name: string;
  rarity_score: number | null;
  case_price: number | null;
  case_volume_30d: number | null;
  supply_growth_30d: number | null;
  drop_pool: string | null;
  rationale: string | null;
  blocking_risk: string | null;
}

export interface FactorRow {
  item_id: string;
  date: string;
  price: number | null;
  dd_from_high: number | null;
  drop_duration_days: number | null;
  ret_7d: number | null;
  ret_21d: number | null;
  ret_30d: number | null;
  vol_21: number | null;
  turnover: number | null;
  list_ratio: number | null;
  list_slope_7d: number | null;
  supply_peer_pct: number | null;
  supply_rigidity: number | null;
  case_rarity_score: number | null;
  rel_ret_21d: number | null;
  event_bearish_30d: number | null;
  event_bullish_30d: number | null;
}

export interface ItemDetail {
  item: {
    item_id: string;
    name: string;
    market_hash_name: string;
    wear: string | null;
    case_id: string | null;
    case_name: string | null;
    image: string | null;
  } | null;
  factor: FactorRow | null;
  forecast: (Candidate & { horizon?: number; model_version?: string }) | null;
  caseMeta: CaseRow | null;
  alerts: { level: string; score: number; ts: number; detail: string }[];
}

export interface ScreenerRow {
  item_id: string;
  name: string | null;
  case_name: string | null;
  image: string | null;
  price: number | null;
  dd_from_high: number | null;
  drop_duration_days: number | null;
  ret_21d: number | null;
  turnover: number | null;
  list_ratio: number | null;
  supply_peer_pct: number | null;
  case_rarity_score: number | null;
  supply_rigidity: number | null;
}

// ------------------------------------------------------------------ 接口
export const api = {
  meta: () => get<Meta>('/api/meta'),
  health: () => get<Record<string, unknown>>('/api/health'),
  marketIndex: (days = 365) => get<IndexRow[]>('/api/market/index', { days }),
  candidates: (p: Params = {}) => get<CandidateResponse>('/api/candidates', p),
  screener: (p: Params = {}) => get<ScreenerRow[]>('/api/screener', p),
  item: (id: string) => get<ItemDetail>(`/api/items/${encodeURIComponent(id)}`),
  kline: (id: string, days = 180) =>
    get<KlineRow[]>(`/api/items/${encodeURIComponent(id)}/kline`, { days }),
  alerts: (p: Params = {}) => get<AlertRow[]>('/api/alerts', p),
  cases: (p: Params = {}) => get<CaseRow[]>('/api/cases', p),
  backtest: () => get<{ date: string; mean_21d: number; cum: number; win_rate: number; n: number }[]>('/api/backtest'),
  search: (q: string) =>
    get<{ item_id: string; name: string; market_hash_name: string; case_name: string | null; image: string | null }[]>(
      '/api/search',
      { q },
    ),
};
