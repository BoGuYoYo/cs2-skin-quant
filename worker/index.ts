/**
 * skin-alpha :: Cloudflare Worker 入口
 *
 * 职责分工（重要）：
 *   - 本 Worker 只做「读 + 缓存 + 接收推送」，不做任何模型计算。
 *   - 训练/推理全部在你自己的服务器（Python）上跑，算完把结果 POST 到 /api/ingest。
 *   - 所以这个 Worker 永远不需要访问你的服务器，服务器也不需要开任何入站端口。
 *     可选的「实时回源」走 UPSTREAM_BASE（Cloudflare Tunnel 的出站通道）。
 */
import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { readRoutes } from './routes/read';
import { ingestRoutes } from './routes/ingest';
import { handleScheduled } from './cron';
import { fail } from './lib/util';

export interface Env {
  DB: D1Database;
  CACHE?: KVNamespace;
  ASSETS: Fetcher;
  /** "true" 时读接口也要求口令（配合 Cloudflare Access 或简易 Basic 口令） */
  REQUIRE_AUTH: string;
  /** 服务器 Tunnel 暴露的只读 API 基地址，留空则纯走 D1 */
  UPSTREAM_BASE: string;
  /** KV 缓存秒数 */
  CACHE_TTL: string;
  /** 服务器推送密钥（wrangler secret put INGEST_TOKEN） */
  INGEST_TOKEN: string;
  /** 可选：网页访问口令 */
  APP_PASSWORD?: string;
}

const app = new Hono<{ Bindings: Env }>();

app.use('/api/*', cors({ origin: '*', allowMethods: ['GET', 'POST', 'OPTIONS'] }));

// ---- 健康检查（不需要鉴权，用于监控和排障） ----
app.get('/api/health', async (c) => {
  const started = Date.now();
  let db = 'down';
  try {
    const r = await c.env.DB.prepare('SELECT 1 AS x').first<{ x: number }>();
    db = r?.x === 1 ? 'up' : 'down';
  } catch {
    db = 'down';
  }
  return c.json({
    ok: true,
    service: 'skin-alpha',
    db,
    cache: c.env.CACHE ? 'bound' : 'absent',
    upstream: c.env.UPSTREAM_BASE ? 'configured' : 'none',
    dbLatencyMs: Date.now() - started,
    now: Date.now(),
  });
});

// ---- 读接口 ----
app.route('/api', readRoutes);

// ---- 推送入口（服务器 → D1） ----
app.route('/api', ingestRoutes);

// ---- 其他 API 路径一律 404（不要落到 SPA fallback） ----
app.all('/api/*', (c) => fail(`no such route: ${new URL(c.req.url).pathname}`, 404));

// ---- 静态资源（SPA） ----
app.all('*', (c) => c.env.ASSETS.fetch(c.req.raw));

export default {
  fetch: app.fetch,
  scheduled: handleScheduled,
} satisfies ExportedHandler<Env>;
