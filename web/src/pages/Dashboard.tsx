import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api';
import { useApi } from '../lib/useApi';
import { useDragOrder } from '../lib/useDragOrder';
import { AreaChart } from '../components/AreaChart';
import { CandidateTable } from '../components/CandidateTable';
import { State } from '../components/State';
import { ProgressPanel } from '../components/ProgressPanel';
import { DraggablePanel } from '../components/DraggablePanel';
import { int, levelTone, pct, relTime } from '../lib/format';

/** 面板顺序可拖拽调整，存 localStorage */
const DEFAULT_ORDER = ['progress', 'candidates', 'market', 'backtest', 'alerts'];

export default function Dashboard() {
  const [editing, setEditing] = useState(false);
  const { order, move, reset } = useDragOrder('skin-alpha:dashboard-order', DEFAULT_ORDER);

  const meta = useApi(() => api.meta(), []);
  const idx = useApi(() => api.marketIndex(365), []);
  const cand = useApi(() => api.candidates({ limit: 12, sort: 'score' }), []);
  const alerts = useApi(() => api.alerts({ limit: 8 }), []);
  const bt = useApi(() => api.backtest(), []);

  const gapDays = meta.data?.latestForecastDate
    ? Math.round((Date.now() - Date.parse(meta.data.latestForecastDate)) / 86400_000)
    : null;
  const btRows = bt.data ?? [];
  const btLast = btRows.length ? btRows[btRows.length - 1] : null;

  const panel = (title: string, en: string, body: ReactNode, tone?: 'ink'): ReactNode => (
    <section className="ark-panel" data-tone={tone}>
      <header className="ark-panelHead">
        <p className="ark-code">{en}</p>
        <h3>{title}</h3>
        {editing && <span className="ark-dragHandle" aria-hidden="true">⠿</span>}
      </header>
      {body}
    </section>
  );

  const panels: Record<string, ReactNode> = {
    progress: <ProgressPanel />,
    candidates: panel(
      '今日候选',
      'DAILY CANDIDATES',
      <State
        loading={cand.loading}
        error={cand.error}
        empty={(cand.data?.rows ?? []).length === 0}
        emptyText="今天没有触发条件的标的 —— 这通常不是坏事"
      >
        <CandidateTable rows={cand.data?.rows ?? []} compact />
      </State>,
    ),
    market: panel(
      '大盘指数',
      'MARKET INDEX / RELATIVE STRENGTH BASE',
      <State loading={idx.loading} error={idx.error} empty={(idx.data ?? []).length === 0}>
        <AreaChart
          data={(idx.data ?? [])
            .filter((r) => r.index_value !== null)
            .map((r) => ({ date: r.date, value: r.index_value as number }))}
          height={190}
        />
      </State>,
    ),
    backtest: panel(
      '回测净值',
      'BACKTEST / TOP-N EQUAL WEIGHT',
      <State loading={bt.loading} error={bt.error} empty={btRows.length === 0}>
        <AreaChart
          data={btRows.map((r) => ({ date: r.date, value: (r.cum + 1) * 100 }))}
          height={190}
          color="#18d1ff"
        />
        {btLast && (
          <p className="ark-dim ark-mono" style={{ fontSize: 11, marginTop: 8 }}>
            样本 {int(btLast.n)} · 胜率 {pct(btLast.win_rate, 0)} · 最新周期 {pct(btLast.mean_21d, 2)}
          </p>
        )}
      </State>,
    ),
    alerts: panel(
      '最新异动',
      'RECENT ANOMALIES',
      <State loading={alerts.loading} error={alerts.error} empty={(alerts.data ?? []).length === 0}>
        <div className="ark-tableWrap" style={{ border: 0 }}>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>等级</th>
                <th>饰品</th>
                <th className="num">打分</th>
              </tr>
            </thead>
            <tbody>
              {(alerts.data ?? []).map((a) => (
                <tr key={a.id}>
                  <td className="ark-dim">{relTime(a.ts)}</td>
                  <td>
                    <span className={`tag t-${levelTone(a.level)}`}>{a.level}</span>
                  </td>
                  <td>
                    <Link to={`/item/${encodeURIComponent(a.item_id)}`}>{a.name ?? a.item_id}</Link>
                  </td>
                  <td className="num">{a.score?.toFixed(0) ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </State>,
    ),
  };

  return (
    <>
      <div className="ark-pageHead">
        <p className="ark-kicker">OPERATION OVERVIEW / 01</p>
        <h1>总览</h1>
        <div className="ark-pageActions">
          <span className="ark-sub ark-mono">
            基准日 {meta.data?.latestForecastDate ?? '—'}
            {gapDays !== null && (
              <>
                {' · '}
                <span className={gapDays >= 3 ? 'ark-neg' : ''}>
                  {gapDays === 0 ? '今日已更新' : `滞后 ${gapDays} 天`}
                </span>
              </>
            )}
          </span>
          <button
            className="ark-btn"
            type="button"
            aria-pressed={editing}
            onClick={() => setEditing((v) => !v)}
          >
            {editing ? '完成' : '排列模块'}
          </button>
        </div>
      </div>

      {gapDays !== null && gapDays >= 3 && (
        <div className="ark-note">
          数据已滞后 {gapDays} 天。检查服务器 <span className="ark-mono">skin-alpha-daily.timer</span> 与推送任务。
        </div>
      )}

      <div className="ark-panel" style={{ marginBottom: '1rem' }}>
        <div className="ark-stats">
          <div className="ark-stat">
            <div className="v">{int(meta.data?.counts?.forecast_today)}</div>
            <div className="l">今日打分标的</div>
          </div>
          <div className="ark-stat">
            <div className="v">{int(meta.data?.counts?.items)}</div>
            <div className="l">在库饰品</div>
          </div>
          <div className="ark-stat">
            <div className="v">{int(meta.data?.counts?.cases)}</div>
            <div className="l">覆盖武器箱</div>
          </div>
          <div className="ark-stat">
            <div className="v">{btLast ? pct(btLast.cum, 1) : '—'}</div>
            <div className="l">回测累计净收益</div>
          </div>
        </div>
      </div>

      {editing && (
        <div className="ark-editBar">
          <span>拖拽任意面板调整顺序</span>
          <button className="ark-btn" type="button" onClick={reset}>
            重置顺序
          </button>
        </div>
      )}

      <div className="ark-grid">
        {order.map((id) => (
          <DraggablePanel
            key={id}
            id={id}
            editing={editing}
            onDropPanel={move}
            className={id === 'progress' || id === 'candidates' ? '' : ''}
          >
            {panels[id] ?? null}
          </DraggablePanel>
        ))}
      </div>
    </>
  );
}
