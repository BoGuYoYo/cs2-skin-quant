import { Hono } from 'hono';
import type { Env } from '../index';
import { ok, fail, withCache, ttl, qInt, qNum, clampStr, qDate } from '../lib/util';
import { isReadAuthorized } from '../lib/auth';

export const readRoutes = new Hono<{ Bindings: Env }>();

// 读接口统一鉴权（REQUIRE_AUTH=false 时直接放行）
readRoutes.use('*', async (c, next) => {
  if (!isReadAuthorized(c.req.raw, c.env)) return fail('unauthorized', 401);
  await next();
});

/** 表名白名单，杜绝 SQL 注入 */
const DATED_TABLES = new Set(['forecast', 'snap_daily', 'kline', 'market_index', 'backtest', 'factor_latest']);

async function latestDate(db: D1Database, table: string): Promise<string | null> {
  if (!DATED_TABLES.has(table)) throw new Error('bad table');
  const r = await db.prepare(`SELECT date AS d FROM ${table} ORDER BY date DESC LIMIT 1`).first<{ d: string }>();
  return r?.d ?? null;
}

const SORT_COLS: Record<string, string> = {
  score: 'f.score',
  q10: 'f.q10_adj',
  prob: 'f.prob_up',
  price: 'fl.price',
  duration: 'fl.drop_duration_days',
  rarity: 'fl.case_rarity_score',
  turnover: 'fl.turnover',
};

// ---------------------------------------------------------------- 元信息
readRoutes.get('/meta', async (c) => {
  const data = await withCache(c.env, 'meta', ttl(c.env), async () => {
    const [metaRows, day, counts] = await Promise.all([
      c.env.DB.prepare('SELECT k, v FROM meta').all<{ k: string; v: string }>(),
      latestDate(c.env.DB, 'forecast'),
      c.env.DB.prepare(
        `SELECT
           (SELECT COUNT(*) FROM items)                AS items,
           (SELECT COUNT(*) FROM case_meta)            AS cases,
           (SELECT COUNT(*) FROM forecast WHERE date = (SELECT MAX(date) FROM forecast)) AS forecast_today,
           (SELECT COUNT(*) FROM alert_view)           AS alerts_total`,
      ).first<Record<string, number>>(),
    ]);
    const meta: Record<string, string> = {};
    for (const r of metaRows.results) meta[r.k] = r.v;
    return { latestForecastDate: day, meta, counts };
  });
  return ok(data);
});

// ---------------------------------------------------------------- 大盘指数
readRoutes.get('/market/index', async (c) => {
  const days = qInt(c.req.query('days'), 365, 7, 3000);
  const key = `mi:${days}`;
  const data = await withCache(c.env, key, ttl(c.env), async () => {
    const r = await c.env.DB.prepare(
      'SELECT date, index_value, online_players, vol_turnover FROM market_index ORDER BY date DESC LIMIT ?',
    )
      .bind(days)
      .all();
    return r.results.reverse();
  });
  return ok(data);
});

// ---------------------------------------------------------------- 候选池
readRoutes.get('/candidates', async (c) => {
  const date = qDate(c.req.query('date'));
  const limit = qInt(c.req.query('limit'), 50, 1, 500);
  const side = clampStr(c.req.query('side'), 10);
  const sortKey = clampStr(c.req.query('sort'), 20) ?? 'score';
  const orderCol = SORT_COLS[sortKey] ?? SORT_COLS.score;
  const dir = c.req.query('dir') === 'asc' ? 'ASC' : 'DESC';
  const minRarity = qNum(c.req.query('rarityMin'));

  const data = await withCache(c.env, `cand:${date ?? 'latest'}:${limit}:${side ?? ''}:${sortKey}:${dir}:${minRarity ?? ''}`, ttl(c.env), async () => {
    const d = date ?? (await latestDate(c.env.DB, 'forecast'));
    if (!d) return { date: null, rows: [] };

    const where: string[] = ['f.date = ?'];
    const binds: unknown[] = [d];
    if (side) {
      where.push('f.side = ?');
      binds.push(side);
    }
    if (minRarity !== undefined) {
      where.push('COALESCE(fl.case_rarity_score, 0) >= ?');
      binds.push(minRarity);
    }
    binds.push(limit);

    const r = await c.env.DB.prepare(
      `SELECT f.item_id, f.q10_adj, f.q50, f.q90_adj, f.prob_up, f.score, f.side, f.size, f.why,
              i.name, i.market_hash_name, i.image, i.case_name, i.wear,
              fl.price, fl.dd_from_high, fl.drop_duration_days, fl.turnover, fl.list_ratio,
              fl.case_rarity_score, fl.rel_ret_21d
         FROM forecast f
         LEFT JOIN items i        ON i.item_id  = f.item_id
         LEFT JOIN factor_latest fl ON fl.item_id = f.item_id
        WHERE ${where.join(' AND ')}
        ORDER BY ${orderCol} ${dir}
        LIMIT ?`,
    )
      .bind(...binds)
      .all();
    return { date: d, rows: r.results };
  });
  return ok(data);
});

// ---------------------------------------------------------------- 自定义筛选
readRoutes.get('/screener', async (c) => {
  const priceMax = qNum(c.req.query('priceMax')) ?? 100;
  const priceMin = qNum(c.req.query('priceMin')) ?? 3;
  const turnoverMin = qNum(c.req.query('turnoverMin')) ?? 0.002;
  const listRatioMax = qNum(c.req.query('listRatioMax')) ?? 0.035;
  const rarityMin = qNum(c.req.query('rarityMin')) ?? 60;
  const limit = qInt(c.req.query('limit'), 100, 1, 500);

  const data = await withCache(
    c.env,
    `scr:${priceMax}:${priceMin}:${turnoverMin}:${listRatioMax}:${rarityMin}:${limit}`,
    ttl(c.env),
    async () => {
      const r = await c.env.DB.prepare(
        `SELECT fl.item_id, fl.price, fl.dd_from_high, fl.drop_duration_days, fl.ret_21d,
                fl.turnover, fl.list_ratio, fl.supply_peer_pct, fl.case_rarity_score, fl.supply_rigidity,
                i.name, i.case_name, i.image
           FROM factor_latest fl
           LEFT JOIN items i ON i.item_id = fl.item_id
          WHERE fl.price BETWEEN ? AND ?
            AND COALESCE(fl.turnover, 0)   >= ?
            AND COALESCE(fl.list_ratio, 0) <= ?
            AND COALESCE(fl.case_rarity_score, 0) >= ?
          ORDER BY fl.case_rarity_score DESC, fl.drop_duration_days DESC
          LIMIT ?`,
      )
        .bind(priceMin, priceMax, turnoverMin, listRatioMax, rarityMin, limit)
        .all();
      return r.results;
    },
  );
  return ok(data);
});

// ---------------------------------------------------------------- 单品详情
readRoutes.get('/items/:id', async (c) => {
  const id = clampStr(c.req.param('id'), 64);
  if (!id) return fail('id required', 400);

  const data = await withCache(c.env, `item:${id}`, ttl(c.env), async () => {
    const [item, factor, forecast, kase, alerts] = await c.env.DB.batch([
      c.env.DB.prepare('SELECT * FROM items WHERE item_id = ?').bind(id),
      c.env.DB.prepare('SELECT * FROM factor_latest WHERE item_id = ?').bind(id),
      c.env.DB.prepare('SELECT * FROM forecast WHERE item_id = ? ORDER BY date DESC LIMIT 1').bind(id),
      c.env.DB.prepare(
        'SELECT cm.* FROM case_meta cm JOIN items i ON i.case_id = cm.case_id WHERE i.item_id = ?',
      ).bind(id),
      c.env.DB.prepare('SELECT level, score, ts, detail FROM alerts WHERE item_id = ? ORDER BY ts DESC LIMIT 20').bind(id),
    ]);
    return {
      item: item.results?.[0] ?? null,
      factor: factor.results?.[0] ?? null,
      forecast: forecast.results?.[0] ?? null,
      caseMeta: kase.results?.[0] ?? null,
      alerts: alerts.results ?? [],
    };
  });
  if (!data.item) return fail('item not found', 404);
  return ok(data);
});

// ---------------------------------------------------------------- 任务进度
// 不缓存：KV 的 expirationTtl 最小 60 秒，缓存会让刚写入的进度消失一分钟。
// 前端 5 秒轮询一次，直接查 D1 的开销完全可以接受（索引走 updated_at）。
readRoutes.get('/progress', async (c) => {
  const r = await c.env.DB.prepare(
    'SELECT * FROM task_progress ORDER BY updated_at DESC LIMIT 20',
  ).all();
  return ok(r.results);
});

// ---------------------------------------------------------------- K 线
readRoutes.get('/items/:id/kline', async (c) => {
  const id = clampStr(c.req.param('id'), 64);
  if (!id) return fail('id required', 400);
  const days = qInt(c.req.query('days'), 180, 7, 2000);

  const data = await withCache(c.env, `kl:${id}:${days}`, ttl(c.env), async () => {
    const r = await c.env.DB.prepare(
      'SELECT date, o, h, l, c, v FROM kline WHERE item_id = ? ORDER BY date DESC LIMIT ?',
    )
      .bind(id, days)
      .all();
    return r.results.reverse();
  });
  return ok(data);
});

// ---------------------------------------------------------------- 告警流
readRoutes.get('/alerts', async (c) => {
  const limit = qInt(c.req.query('limit'), 100, 1, 500);
  const level = clampStr(c.req.query('level'), 4);
  const data = await withCache(c.env, `al:${limit}:${level ?? ''}`, ttl(c.env), async () => {
    const r = level
      ? await c.env.DB.prepare('SELECT * FROM alert_view WHERE level = ? ORDER BY ts DESC LIMIT ?').bind(level, limit).all()
      : await c.env.DB.prepare('SELECT * FROM alert_view ORDER BY ts DESC LIMIT ?').bind(limit).all();
    return r.results;
  });
  return ok(data);
});

// ---------------------------------------------------------------- 箱子稀有度榜
readRoutes.get('/cases', async (c) => {
  const limit = qInt(c.req.query('limit'), 100, 1, 500);
  const sort = c.req.query('sort') === 'volume' ? 'case_volume_30d DESC' : 'rarity_score DESC';
  const data = await withCache(c.env, `cs:${limit}:${sort}`, ttl(c.env), async () => {
    const r = await c.env.DB.prepare(
      `SELECT * FROM case_meta ORDER BY ${sort} LIMIT ?`,
    )
      .bind(limit)
      .all();
    return r.results;
  });
  return ok(data);
});

// ---------------------------------------------------------------- 回测曲线
readRoutes.get('/backtest', async (c) => {
  const data = await withCache(c.env, 'bt', ttl(c.env), async () => {
    const r = await c.env.DB.prepare('SELECT * FROM backtest ORDER BY date ASC').all();
    return r.results;
  });
  return ok(data);
});

// ---------------------------------------------------------------- 搜索
readRoutes.get('/search', async (c) => {
  const q = clampStr(c.req.query('q'), 60);
  if (!q || q.length < 2) return ok([]);
  const like = `%${q}%`;
  const r = await c.env.DB.prepare(
    `SELECT item_id, name, market_hash_name, case_name, image
       FROM items
      WHERE name LIKE ? OR market_hash_name LIKE ?
      LIMIT 30`,
  )
    .bind(like, like)
    .all();
  return ok(r.results);
});
