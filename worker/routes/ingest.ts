/**
 * 推送入口：服务器（Python 管线）唯一需要调用的写接口。
 *
 *   POST /api/ingest
 *   Authorization: Bearer <INGEST_TOKEN>
 *   { "stage": "forecast", "rows": [ {...}, ... ], "replaceDate": "2026-10-11" }
 *
 * 设计要点：
 *   - 每个 stage 的列清单写死在这里，客户端不能自由指定列（防注入、防脏数据）
 *   - D1 免费档 batch 上限 1000 条语句，自动分块
 *   - 支持 replaceDate：先删同日旧数据再插入，保证幂等重跑
 */
import { Hono } from 'hono';
import type { Env } from '../index';
import { ok, fail } from '../lib/util';
import { isIngestAuthorized } from '../lib/auth';

export const ingestRoutes = new Hono<{ Bindings: Env }>();

type Stage = { table: string; cols: string[]; pk: string[] };

const STAGES: Record<string, Stage> = {
  items: {
    table: 'items',
    cols: ['item_id', 'name', 'market_hash_name', 'wear', 'case_id', 'case_name', 'image', 'updated_at'],
    pk: ['item_id'],
  },
  case_meta: {
    table: 'case_meta',
    cols: ['case_id', 'case_name', 'rarity_score', 'case_price', 'case_volume_30d', 'supply_growth_30d', 'drop_pool', 'rationale', 'blocking_risk', 'updated_at'],
    pk: ['case_id'],
  },
  snap_daily: {
    table: 'snap_daily',
    cols: ['item_id', 'date', 'open', 'high', 'low', 'close', 'bid', 'listings', 'volume', 'supply'],
    pk: ['item_id', 'date'],
  },
  kline: {
    table: 'kline',
    cols: ['item_id', 'date', 'o', 'h', 'l', 'c', 'v'],
    pk: ['item_id', 'date'],
  },
  forecast: {
    table: 'forecast',
    cols: ['item_id', 'date', 'horizon', 'q10', 'q50', 'q90', 'q10_adj', 'q90_adj', 'prob_up', 'fwd_max_dd', 'score', 'side', 'size', 'why', 'model_version'],
    pk: ['item_id', 'date'],
  },
  factor_latest: {
    table: 'factor_latest',
    cols: ['item_id', 'date', 'price', 'dd_from_high', 'drop_duration_days', 'ret_7d', 'ret_21d', 'ret_30d', 'vol_21', 'turnover', 'list_ratio', 'list_slope_7d', 'supply_peer_pct', 'supply_rigidity', 'case_rarity_score', 'rel_ret_21d', 'event_bearish_30d', 'event_bullish_30d', 'updated_at'],
    pk: ['item_id'],
  },
  alerts: {
    table: 'alerts',
    cols: ['item_id', 'name', 'level', 'score', 'ts', 'detail', 'pushed'],
    pk: [],
  },
  market_index: {
    table: 'market_index',
    cols: ['date', 'index_value', 'online_players', 'vol_turnover'],
    pk: ['date'],
  },
  backtest: {
    table: 'backtest',
    cols: ['date', 'mean_21d', 'cum', 'win_rate', 'n', 'model_version'],
    pk: ['date'],
  },
  meta: {
    table: 'meta',
    cols: ['k', 'v', 'updated_at'],
    pk: ['k'],
  },
  runs: {
    table: 'runs',
    cols: ['ts', 'stage', 'ok', 'rows', 'message'],
    pk: [],
  },
};

const BATCH_MAX = 900; // 免费档 1000，留出余量

function insertSql(stage: Stage): string {
  const ph = stage.cols.map(() => '?').join(',');
  return `INSERT OR REPLACE INTO ${stage.table} (${stage.cols.join(',')}) VALUES (${ph})`;
}

function rowToBind(row: Record<string, unknown>, stage: Stage): unknown[] {
  return stage.cols.map((col) => {
    const v = row[col];
    if (v === undefined || v === null) return null;
    if (typeof v === 'boolean') return v ? 1 : 0;
    if (typeof v === 'object') return JSON.stringify(v);
    return v as string | number;
  });
}

ingestRoutes.post('/ingest', async (c) => {
  if (!isIngestAuthorized(c.req.raw, c.env)) return fail('unauthorized', 401);

  let body: { stage?: string; rows?: unknown[]; replaceDate?: string; replaceSeries?: string };
  try {
    body = await c.req.json();
  } catch {
    return fail('invalid json', 400);
  }

  const stageName = String(body.stage ?? '');
  const stage = STAGES[stageName];
  if (!stage) return fail(`unknown stage: ${stageName} (allowed: ${Object.keys(STAGES).join(', ')})`, 400);

  const rows = Array.isArray(body.rows) ? body.rows : [];
  if (rows.length === 0) return ok({ inserted: 0 });

  const db = c.env.DB;
  let deleted = 0;

  // 幂等重跑：先删掉同日/同序列旧数据
  if (body.replaceDate && stage.cols.includes('date')) {
    const del = await db.prepare(`DELETE FROM ${stage.table} WHERE date = ?`).bind(body.replaceDate).run();
    deleted = del.meta.changes ?? 0;
  }
  if (body.replaceSeries && stage.cols.includes('item_id')) {
    const del = await db.prepare(`DELETE FROM ${stage.table} WHERE item_id = ?`).bind(body.replaceSeries).run();
    deleted += del.meta.changes ?? 0;
  }

  const stmt = insertSql(stage);
  let inserted = 0;
  for (let i = 0; i < rows.length; i += BATCH_MAX) {
    const chunk = rows.slice(i, i + BATCH_MAX) as Record<string, unknown>[];
    const prepared = chunk.map((r) => db.prepare(stmt).bind(...rowToBind(r, stage)));
    const results = await db.batch(prepared);
    for (const res of results) inserted += res.meta.changes ?? 0;
  }

  await db
    .prepare('INSERT INTO runs (ts, stage, ok, rows, message) VALUES (?,?,?,?,?)')
    .bind(Date.now(), `ingest:${stageName}`, 1, inserted, `deleted=${deleted}`)
    .run();

  return ok({ stage: stageName, received: rows.length, inserted, deleted });
});
