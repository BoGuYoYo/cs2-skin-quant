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
  task_progress: {
    table: 'task_progress',
    cols: ['task', 'status', 'total', 'done', 'ok', 'empty', 'fail',
           'rows', 'rate_per_min', 'eta_minutes', 'message', 'started_at', 'updated_at'],
    pk: ['task'],
  },
  runs: {
    table: 'runs',
    cols: ['ts', 'stage', 'ok', 'rows', 'message'],
    pk: [],
  },
};

const BATCH_MAX = 900; // 免费档 1000，留出余量

/**
 * 允许「整表替换」的 stage（先清空再插入）。
 *
 * 只有语义上就是「当前快照」的表才配得上：
 *   factor_latest —— 当前截面。云端此前只做逐行 upsert，被流动性闸门/新鲜度闸门
 *   剔除的品种那一行会永远留着，而 /screener 又不带日期条件 —— 旧行会被当成
 *   当前扫描结果一直展示（本地已经修掉，云端副本漏了）。
 * 白名单而不是随便什么表都能清：一个误传的布尔值不该能把历史表抹掉。
 */
const REPLACE_ALL_STAGES = new Set(['factor_latest', 'backtest']);

const COMPLETE_STAGES = new Set(['items', 'case_meta', 'snap_daily', 'kline', 'forecast', 'factor_latest', 'alerts', 'runs']);

function colsForRow(stage: Stage, row: Record<string, unknown>): string[] {
  if (COMPLETE_STAGES.has(stage.table)) return stage.cols;
  return stage.cols.filter((col) => Object.prototype.hasOwnProperty.call(row, col) && row[col] !== null);
}

function insertSql(stage: Stage, cols: string[]): string {
  const ph = cols.map(() => '?').join(',');
  if (stage.pk.length > 0 && cols.some((c) => stage.pk.includes(c))) {
    const updates = cols.filter((c) => !stage.pk.includes(c));
    if (updates.length > 0) {
      const sets = updates.map((c) => `${c}=excluded.${c}`).join(',');
      return `INSERT INTO ${stage.table} (${cols.join(',')}) VALUES (${ph}) ON CONFLICT(${stage.pk.join(',')}) DO UPDATE SET ${sets}`;
    }
    return `INSERT OR IGNORE INTO ${stage.table} (${cols.join(',')}) VALUES (${ph})`;
  }
  return `INSERT OR REPLACE INTO ${stage.table} (${cols.join(',')}) VALUES (${ph})`;
}

function rowToBind(row: Record<string, unknown>, cols: string[]): unknown[] {
  return cols.map((col) => {
    const v = row[col];
    if (v === undefined || v === null) return null;
    if (typeof v === 'boolean') return v ? 1 : 0;
    if (typeof v === 'object') return JSON.stringify(v);
    return v as string | number;
  });
}

ingestRoutes.post('/ingest', async (c) => {
  if (!isIngestAuthorized(c.req.raw, c.env)) return fail('unauthorized', 401);

  let body: {
    stage?: string;
    rows?: unknown[];
    replaceDate?: string;
    replaceSeries?: string;
    replaceSince?: number;
    replaceAll?: boolean;
  };
  try {
    body = await c.req.json();
  } catch {
    return fail('invalid json', 400);
  }

  const stageName = String(body.stage ?? '');
  const stage = STAGES[stageName];
  if (!stage) return fail(`unknown stage: ${stageName} (allowed: ${Object.keys(STAGES).join(', ')})`, 400);

  const rows = Array.isArray(body.rows) ? body.rows : [];

  const db = c.env.DB;
  let deleted = 0;

  // ⚠️ 空批次也必须走完下面的删除（审查 9 发现）。
  // 「本轮没有行」≠「本轮不该有行」：原先 `rows.length === 0` 在这里直接返回，
  // 于是 alerts 的 replaceSince / forecast 的 replaceDate / factor_latest 的
  // replaceAll 在空批次下全部丢失 —— 服务器本地已经撤回的告警与买入建议
  // 会在 D1 里继续挂着，两个看板各说各话。
  // 现在顺序是「先删 → 再看有没有行要插」。
  if (body.replaceAll === true) {
    if (!REPLACE_ALL_STAGES.has(stageName)) {
      return fail(`stage ${stageName} 不支持 replaceAll`, 400);
    }
    const del = await db.prepare(`DELETE FROM ${stage.table}`).run();
    deleted = del.meta.changes ?? 0;
  }

  // 幂等重跑：先删掉同日/同序列旧数据
  if (body.replaceDate && stage.cols.includes('date')) {
    const del = await db.prepare(`DELETE FROM ${stage.table} WHERE date = ?`).bind(body.replaceDate).run();
    deleted = del.meta.changes ?? 0;
  }
  if (body.replaceSeries && stage.cols.includes('item_id')) {
    const del = await db.prepare(`DELETE FROM ${stage.table} WHERE item_id = ?`).bind(body.replaceSeries).run();
    deleted += del.meta.changes ?? 0;
  }
  // 只有 ts、没有 date 的表（alerts）：按时间戳划界替换。
  // 踩过的坑：alerts 的 pk 是 [] + INSERT OR REPLACE，没有冲突键就退化成普通插入，
  // 而 replaceDate 又只对含 date 的表生效 —— 一天跑两次云端告警就翻倍。
  if (typeof body.replaceSince === 'number' && stage.cols.includes('ts')) {
    const del = await db
      .prepare(`DELETE FROM ${stage.table} WHERE ts >= ?`)
      .bind(body.replaceSince)
      .run();
    deleted += del.meta.changes ?? 0;
  }

  let inserted = 0;
  if (rows.length > 0) {
    for (let i = 0; i < rows.length; i += BATCH_MAX) {
      const chunk = rows.slice(i, i + BATCH_MAX) as Record<string, unknown>[];
      const prepared = chunk.flatMap((r) => {
        const cols = colsForRow(stage, r);
        if (cols.length === 0) return [];
        return [db.prepare(insertSql(stage, cols)).bind(...rowToBind(r, cols))];
      });
      if (prepared.length === 0) continue;
      const results = await db.batch(prepared);
      for (const res of results) inserted += res.meta.changes ?? 0;
    }
  }

  await db
    .prepare('INSERT INTO runs (ts, stage, ok, rows, message) VALUES (?,?,?,?,?)')
    .bind(Math.floor(Date.now() / 1000), `ingest:${stageName}`, 1, inserted, `deleted=${deleted}`)
    .run();

  // 写完立刻让读缓存失效。
  // 不能靠短 TTL —— KV 的 expirationTtl 最小值是 60 秒（withCache 里被 Math.max(60,..) 夹住），
  // 意味着一次空结果会被缓存整整一分钟，前端就看不到刚写入的进度（踩过）。
  if (c.env.CACHE) {
    try {
      const exact: string[] = [];
      const prefixes: string[] = [];
      if (['forecast', 'factor_latest', 'items', 'case_meta'].includes(stageName)) exact.push('meta');
      if (['forecast', 'factor_latest', 'items', 'case_meta'].includes(stageName)) prefixes.push('cand:');
      if (['factor_latest', 'items', 'case_meta'].includes(stageName)) prefixes.push('scr:');
      if (['forecast', 'factor_latest', 'items', 'case_meta'].includes(stageName)) prefixes.push('item:');
      if (['items', 'case_meta'].includes(stageName)) prefixes.push('cs:');
      if (stageName === 'alerts') prefixes.push('al:', 'item:');
      if (stageName === 'backtest') exact.push('bt');
      if (stageName === 'market_index') prefixes.push('mi:');
      if (stageName === 'meta') exact.push('meta');
      if (exact.length > 0) {
        await Promise.all(exact.map((k) => c.env.CACHE!.delete(k).catch(() => undefined)));
      }
      for (const prefix of prefixes) {
        let cursor: string | undefined;
        do {
          const listed = await c.env.CACHE!.list({ prefix, cursor });
          await Promise.all(listed.keys.map((k) => c.env.CACHE!.delete(k.name).catch(() => undefined)));
          cursor = listed.list_complete ? undefined : listed.cursor;
        } while (cursor);
      }
    } catch {
      // 缓存失效失败不能让已落库的写入被客户端重试。
    }
  }

  return ok({ stage: stageName, received: rows.length, inserted, deleted });
});
