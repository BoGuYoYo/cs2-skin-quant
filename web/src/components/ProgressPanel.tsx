import { useEffect, useState } from 'react';
import { api, type TaskProgress } from '../lib/api';

const TASK_LABEL: Record<string, { zh: string; en: string }> = {
  backfill: { zh: '历史回填', en: 'HISTORY BACKFILL' },
  collect: { zh: '行情采集', en: 'MARKET COLLECT' },
  listings: { zh: '挂单量采集', en: 'LISTINGS POLL' },
  train: { zh: '模型训练', en: 'MODEL TRAIN' },
  daily: { zh: '每日管线', en: 'DAILY PIPELINE' },
};

const STATUS_TONE: Record<string, string> = {
  running: 'is-running',
  done: 'is-done',
  failed: 'is-failed',
  idle: 'is-idle',
};

/**
 * 「多久没上报就算断了」。
 *
 * 阈值取上报间隔的 3~5 倍：挂单量采集器一分钟一报（常驻循环），
 * 采集/回填按 20 秒节流上报，训练是一次性长任务所以放宽到 30 分钟。
 * 原先面板只看 status 字段 —— 进程死了以后那一行永远停在 running，
 * 面板照样显示 RUNNING，属于「看板说谎」（审查 9 发现）。
 */
const STALE_AFTER: Record<string, number> = {
  listings: 5 * 60,
  collect: 10 * 60,
  backfill: 10 * 60,
  train: 30 * 60,
};
const STALE_DEFAULT = 30 * 60;

function staleAfterOf(task: string): number {
  return STALE_AFTER[task] ?? STALE_DEFAULT;
}

function isStale(r: TaskProgress): boolean {
  if (r.status !== 'running') return false;
  const age = r.age_seconds;
  return typeof age === 'number' && age > staleAfterOf(r.task);
}

function fmtAge(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || !Number.isFinite(sec)) return '—';
  if (sec < 60) return `${Math.round(sec)} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟`;
  return `${Math.floor(sec / 3600)} 小时`;
}

function fmtDuration(min: number | null): string {
  if (min === null || !Number.isFinite(min)) return '—';
  if (min < 1) return '< 1 分钟';
  if (min < 60) return `${Math.round(min)} 分钟`;
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return `${h} 小时 ${m} 分`;
}

/**
 * 任务进度面板 —— 每 5 秒轮询 /api/progress。
 *
 * 数据由服务器侧 push/progress.py 写入 D1 的 task_progress 表，
 * 回填/采集/训练各自 upsert 一行当前快照，所以这里永远读的是最新状态。
 */
export function ProgressPanel() {
  const [rows, setRows] = useState<TaskProgress[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await api.progress();
        if (alive) {
          setRows(data);
          setErr(null);
        }
      } catch (e) {
        if (alive) setErr((e as Error).message);
      }
    };
    void load();
    const id = window.setInterval(load, 5000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const hasRunning = rows.some((r) => r.status === 'running' && !isStale(r));
  const hasStale = rows.some(isStale);

  return (
    <section className="ark-panel ark-progress" aria-live="polite">
      <header className="ark-panelHead">
        <p className="ark-code">TASK MONITOR / {String(rows.length).padStart(2, '0')}</p>
        <h3>任务进度</h3>
        <span className={`ark-live ${hasRunning ? 'is-active' : ''}`} data-testid="progress-live">
          <i aria-hidden="true" />
          {err ? 'OFFLINE' : hasRunning ? 'RUNNING' : hasStale ? 'STALE' : 'IDLE'}
        </span>
      </header>

      {err && <p className="ark-error">进度接口不可用：{err}</p>}

      {!err && rows.length === 0 && (
        <p className="ark-empty">暂无任务记录 —— 管线还没上报过进度</p>
      )}

      <div className="ark-progressList">
        {rows.map((r) => {
          const total = r.total ?? 0;
          const done = r.done ?? 0;
          const pct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
          const label = TASK_LABEL[r.task] ?? { zh: r.task, en: r.task.toUpperCase() };
          const stale = isStale(r);
          const shownStatus = stale ? '疑似中断' : r.status;
          return (
            <article key={r.task} className="ark-progressItem" data-status={stale ? 'failed' : r.status}>
              <div className="ark-progressRow">
                <span className="ark-progressName">
                  <strong>{label.zh}</strong>
                  <small>{label.en}</small>
                </span>
                <span className={`ark-tag ${stale ? 'is-failed' : STATUS_TONE[r.status] ?? ''}`}>
                  {shownStatus}
                </span>
              </div>

              <div
                className="ark-bar"
                role="progressbar"
                aria-valuenow={Math.round(pct)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`${label.zh}进度`}
              >
                <i style={{ width: `${r.status === 'running' ? pct : pct || (r.status === 'done' ? 100 : 0)}%` }} />
              </div>

              <dl className="ark-progressMeta">
                <div>
                  <dt>进度</dt>
                  <dd>
                    {done.toLocaleString('zh-CN')}
                    {total > 0 && <span> / {total.toLocaleString('zh-CN')}</span>}
                    {total > 0 && <em>{pct.toFixed(1)}%</em>}
                  </dd>
                </div>
                <div>
                  <dt>速率</dt>
                  <dd>{r.rate_per_min ? `${r.rate_per_min.toFixed(0)} 个/分` : '—'}</dd>
                </div>
                <div>
                  <dt>剩余</dt>
                  <dd>{r.status === 'running' ? fmtDuration(r.eta_minutes) : '—'}</dd>
                </div>
                <div>
                  <dt>写入</dt>
                  <dd>{r.rows !== null ? r.rows.toLocaleString('zh-CN') : '—'}</dd>
                </div>
                <div>
                  <dt>空 / 失败</dt>
                  <dd>
                    <span className="ark-dim">{r.empty ?? 0}</span>
                    {' / '}
                    <span className={(r.fail ?? 0) > 0 ? 'ark-neg' : 'ark-dim'}>{r.fail ?? 0}</span>
                  </dd>
                </div>
                <div>
                  <dt>更新</dt>
                  <dd>
                    {new Date(r.updated_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}
                    {r.age_seconds !== null && r.age_seconds !== undefined && (
                      <span className="ark-dim"> · {fmtAge(r.age_seconds)}前</span>
                    )}
                  </dd>
                </div>
              </dl>

              {stale && (
                <p className="ark-progressMsg">
                  已 {fmtAge(r.age_seconds)} 没上报（正常应每 {Math.round(staleAfterOf(r.task) / 60)}{' '}
                  分钟内一次）—— 进程可能已经退出
                  {r.task === 'listings' ? '：systemctl status skin-alpha-listings' : ''}
                </p>
              )}
              {r.message && <p className="ark-progressMsg">{r.message}</p>}
            </article>
          );
        })}
      </div>
    </section>
  );
}

/** 顶部一行精简状态，放在 shell 的 topbar 里 */
export function ProgressStrip() {
  const [rows, setRows] = useState<TaskProgress[]>([]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await api.progress();
        if (alive) setRows(data.filter((r) => r.status === 'running'));
      } catch {
        /* topbar 状态不报错 */
      }
    };
    void load();
    const id = window.setInterval(load, 8000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  if (rows.length === 0) return null;
  const r = rows[0];
  const pct = r.total ? ((r.done ?? 0) / r.total) * 100 : 0;
  const label = TASK_LABEL[r.task]?.zh ?? r.task;
  return (
    <span className="ark-strip" title={r.message ?? ''}>
      <i aria-hidden="true" />
      {label} {pct.toFixed(0)}%
    </span>
  );
}
