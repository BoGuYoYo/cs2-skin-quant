/**
 * Cron：每日 UTC 02:00（北京时间 10:00）触发。
 *
 * 注意一个常见误解：Cron 不能替你做模型计算（Workers 跑不了 Python）。
 * 它在这里只做三件事：
 *   1) 数据维护（清理过期 K 线/告警，控制 D1 5GB 存储）
 *   2) 可选：通过 Tunnel 回源触发服务器跑当日管线（服务器主动拉更常见，二者取一）
 *   3) 健康巡检 + 把结果写进 runs 表，前端可以看到「管线是否还活着」
 */
import type { Env } from './index';

const KLINE_RETAIN_DAYS = 800;
const ALERT_RETAIN_DAYS = 180;
const RUN_RETAIN_DAYS = 30;

function beijingDate(ts = Date.now()): string {
  return new Date(ts + 8 * 3600_000).toISOString().slice(0, 10);
}

async function runDaily(env: Env, cron: string): Promise<void> {
  const started = Date.now();
  const notes: string[] = [];

  try {
    const cutoffKline = beijingDate(Date.now() - KLINE_RETAIN_DAYS * 86400_000);
    const d1 = await env.DB.prepare('DELETE FROM kline WHERE date < ?').bind(cutoffKline).run();
    notes.push(`kline_pruned=${d1.meta.changes ?? 0}`);

    const cutoffAlert = Math.floor((Date.now() - ALERT_RETAIN_DAYS * 86400_000) / 1000);
    const d2 = await env.DB.prepare('DELETE FROM alerts WHERE ts < ?').bind(cutoffAlert).run();
    notes.push(`alerts_pruned=${d2.meta.changes ?? 0}`);

    // runs 是纯日志表：每个 ingest 都写一行、cron 自己也写一行，只增不减
    // （审查发现）。不清的话 D1 存储会一直被日志吃掉。
    const cutoffRun = Math.floor((Date.now() - RUN_RETAIN_DAYS * 86400_000) / 1000);
    const cutoffRunFuture = Math.floor(Date.now() / 1000) + 86400;
    const d3 = await env.DB.prepare('DELETE FROM runs WHERE ts < ? OR ts > ?')
      .bind(cutoffRun, cutoffRunFuture)
      .run();
    notes.push(`runs_pruned=${d3.meta.changes ?? 0}`);
  } catch (e) {
    notes.push(`prune_failed=${(e as Error).message}`);
  }

  // 可选：回源触发服务器重算（需要配 UPSTREAM_BASE + INGEST_TOKEN）
  if (env.UPSTREAM_BASE) {
    try {
      const res = await fetch(`${env.UPSTREAM_BASE.replace(/\/$/, '')}/api/admin/refresh`, {
        method: 'POST',
        headers: { authorization: `Bearer ${env.INGEST_TOKEN}`, 'content-type': 'application/json' },
        body: JSON.stringify({ trigger: 'cron', cron }),
      });
      notes.push(`upstream_trigger=${res.status}`);
    } catch (e) {
      notes.push(`upstream_failed=${(e as Error).message}`);
    }
  }

  // 巡检：最新预测日期是否落后
  try {
    const r = await env.DB.prepare('SELECT MAX(date) AS d FROM forecast').first<{ d: string | null }>();
    const latest = r?.d ?? null;
    const today = beijingDate();
    if (latest) {
      const gap = Math.round((Date.parse(today) - Date.parse(latest)) / 86400_000);
      notes.push(`forecast_latest=${latest} gap_days=${gap}`);
      if (gap >= 3) notes.push('WARN: 管线可能已停摆');
    } else {
      notes.push('WARN: forecast 表为空');
    }
  } catch (e) {
    notes.push(`health_failed=${(e as Error).message}`);
  }

  await env.DB.prepare('INSERT INTO runs (ts, stage, ok, rows, message) VALUES (?,?,?,?,?)')
    .bind(Math.floor(Date.now() / 1000), 'cron', 1, 0, `${notes.join(' | ')} (${Date.now() - started}ms)`)
    .run();
}

export async function handleScheduled(
  controller: ScheduledController,
  env: Env,
  ctx: ExecutionContext,
): Promise<void> {
  ctx.waitUntil(runDaily(env, controller.cron));
}
