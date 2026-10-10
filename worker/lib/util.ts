import type { Env } from '../index';

export function json(data: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(data), {
    ...init,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      ...(init.headers as Record<string, string> | undefined),
    },
  });
}

export function ok<T>(data: T, extra: Record<string, unknown> = {}): Response {
  return json({ ok: true, data, ...extra });
}

export function fail(message: string, status = 400, extra: Record<string, unknown> = {}): Response {
  return json({ ok: false, error: message, ...extra }, { status });
}

/** 数字查询参数解析，带上下界，避免 D1 收到离谱的 LIMIT */
export function qInt(v: string | undefined, def: number, min: number, max: number): number {
  const n = Number.parseInt(v ?? '', 10);
  if (!Number.isFinite(n)) return def;
  return Math.min(Math.max(n, min), max);
}

export function qNum(v: string | undefined): number | undefined {
  if (v === undefined || v === '') return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}

export function clampStr(v: string | undefined, max = 200): string | undefined {
  if (!v) return undefined;
  return v.slice(0, max);
}

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

export function qDate(v: string | undefined): string | undefined {
  const s = clampStr(v, 10);
  return s && DAY_RE.test(s) ? s : undefined;
}

/**
 * KV 读缓存。
 * 注意：CACHE 未绑定时静默降级为直查，不要让它成为硬依赖。
 */
export async function withCache<T>(
  env: Env,
  key: string,
  ttlSeconds: number,
  producer: () => Promise<T>,
): Promise<T> {
  if (!env.CACHE) return producer();
  try {
    const hit = (await env.CACHE.get(key, 'json')) as T | null;
    if (hit !== null && hit !== undefined) return hit;
  } catch {
    /* 缓存读失败不该影响主流程 */
  }
  const fresh = await producer();
  try {
    await env.CACHE.put(key, JSON.stringify(fresh), { expirationTtl: Math.max(60, ttlSeconds) });
  } catch {
    /* 忽略 */
  }
  return fresh;
}

export function ttl(env: Env): number {
  const n = Number.parseInt(env.CACHE_TTL ?? '', 10);
  return Number.isFinite(n) && n > 0 ? n : 60;
}
