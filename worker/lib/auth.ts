import type { Env } from '../index';

/** 常数时间字符串比较，避免 token 逐字符比较造成时序泄漏 */
export function safeEqual(a: string, b: string): boolean {
  const ae = new TextEncoder().encode(a);
  const be = new TextEncoder().encode(b);
  if (ae.length !== be.length) return false;
  let diff = 0;
  for (let i = 0; i < ae.length; i++) diff |= ae[i] ^ be[i];
  return diff === 0;
}

export function bearer(request: Request): string {
  const h = request.headers.get('authorization') ?? '';
  return h.toLowerCase().startsWith('bearer ') ? h.slice(7).trim() : '';
}

export function isIngestAuthorized(request: Request, env: Env): boolean {
  if (!env.INGEST_TOKEN) return false; // 未配置密钥时彻底关闭写入口
  return safeEqual(bearer(request), env.INGEST_TOKEN);
}

/** 可选的网页口令：REQUIRE_AUTH=true 且配置 APP_PASSWORD 时生效 */
export function isReadAuthorized(request: Request, env: Env): boolean {
  if (env.REQUIRE_AUTH !== 'true' || !env.APP_PASSWORD) return true;
  const url = new URL(request.url);
  const qp = url.searchParams.get('pw');
  if (qp && safeEqual(qp, env.APP_PASSWORD)) return true;
  const hdr = request.headers.get('x-app-password') ?? '';
  if (safeEqual(hdr, env.APP_PASSWORD)) return true;
  const h = request.headers.get('authorization') ?? '';
  if (h.toLowerCase().startsWith('basic ')) {
    try {
      const decoded = atob(h.slice(6));
      const idx = decoded.indexOf(':');
      if (idx >= 0 && safeEqual(decoded.slice(idx + 1), env.APP_PASSWORD)) return true;
    } catch {
      /* 忽略 */
    }
  }
  return false;
}
