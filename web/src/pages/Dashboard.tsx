import { Link } from 'react-router-dom';
import { api } from '../lib/api';
import { useApi } from '../lib/useApi';
import { AreaChart } from '../components/AreaChart';
import { CandidateTable } from '../components/CandidateTable';
import { State } from '../components/State';
import { int, levelTone, money, pct, relTime } from '../lib/format';

export default function Dashboard() {
  const meta = useApi(() => api.meta(), []);
  const idx = useApi(() => api.marketIndex(365), []);
  const cand = useApi(() => api.candidates({ limit: 12, sort: 'score' }), []);
  const alerts = useApi(() => api.alerts({ limit: 8 }), []);
  const bt = useApi(() => api.backtest(), []);

  const gapDays =
    meta.data?.latestForecastDate
      ? Math.round((Date.now() - Date.parse(meta.data.latestForecastDate)) / 86400_000)
      : null;

  const btRows = bt.data ?? [];
  const btLast = btRows.length ? btRows[btRows.length - 1] : null;

  return (
    <>
      <div className="topbar">
        <div>
          <h1>总览</h1>
          <div className="sub">
            基准日 <span className="mono">{meta.data?.latestForecastDate ?? '—'}</span>
            {gapDays !== null && (
              <>
                {' · '}
                <span className={gapDays >= 3 ? 'neg' : 'dim'}>
                  {gapDays === 0 ? '今日已更新' : `滞后 ${gapDays} 天`}
                </span>
              </>
            )}
            {meta.data?.meta?.model_version ? ` · 模型 ${meta.data.meta.model_version}` : ''}
          </div>
        </div>
        <div className="badge-row">
          <Link className="tag t-mute" to="/candidates">
            全部候选池 →
          </Link>
        </div>
      </div>

      {gapDays !== null && gapDays >= 3 && (
        <div className="note">
          管线已停摆 {gapDays} 天。检查服务器上的 <span className="mono">run_daily.py</span> 与推送任务是否还在跑。
        </div>
      )}

      <div className="grid g4" style={{ marginBottom: 14 }}>
        <div className="card stat">
          <div className="v">{int(meta.data?.counts?.forecast_today)}</div>
          <div className="l">今日打分标的</div>
        </div>
        <div className="card stat">
          <div className="v">{int(meta.data?.counts?.items)}</div>
          <div className="l">在库饰品</div>
        </div>
        <div className="card stat">
          <div className="v">{int(meta.data?.counts?.cases)}</div>
          <div className="l">覆盖武器箱</div>
        </div>
        <div className="card stat">
          <div className="v">{btLast ? pct(btLast.cum, 1) : '—'}</div>
          <div className="l">回测累计净收益（21日持仓滚动）</div>
        </div>
      </div>

      <div className="grid g2" style={{ marginBottom: 14 }}>
        <div className="card">
          <h3>大盘指数（相对强度基准）</h3>
          <State loading={idx.loading} error={idx.error} empty={(idx.data ?? []).length === 0}>
            <AreaChart
              data={(idx.data ?? [])
                .filter((r) => r.index_value !== null)
                .map((r) => ({ date: r.date, value: r.index_value as number }))}
              height={220}
            />
          </State>
        </div>
        <div className="card">
          <h3>回测净值（决策层，Top 候选等权）</h3>
          <State loading={bt.loading} error={bt.error} empty={btRows.length === 0}>
            <AreaChart
              data={btRows.map((r) => ({ date: r.date, value: (r.cum + 1) * 100 }))}
              height={220}
              color="#2ecc8f"
            />
            {btLast && (
              <div className="dim" style={{ fontSize: 12, marginTop: 6 }}>
                样本 {int(btLast.n)} · 胜率 {pct(btLast.win_rate, 0)} · 最新单周期 {pct(btLast.mean_21d, 2)}
              </div>
            )}
          </State>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>今日候选（按综合打分）</h3>
        <State loading={cand.loading} error={cand.error} empty={(cand.data?.rows ?? []).length === 0}
               emptyText="今天没有触发条件的标的——这通常不是坏事">
          <CandidateTable rows={cand.data?.rows ?? []} compact />
        </State>
      </div>

      <div className="card">
        <h3>最新异动</h3>
        <State loading={alerts.loading} error={alerts.error} empty={(alerts.data ?? []).length === 0}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>等级</th>
                  <th>饰品</th>
                  <th className="num">打分</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {(alerts.data ?? []).map((a) => (
                  <tr key={a.id}>
                    <td className="dim">{relTime(a.ts)}</td>
                    <td>
                      <span className={`tag t-${levelTone(a.level)}`}>{a.level}</span>
                    </td>
                    <td>
                      <Link to={`/item/${encodeURIComponent(a.item_id)}`}>{a.name ?? a.item_id}</Link>
                    </td>
                    <td className="num">{a.score?.toFixed(0) ?? '—'}</td>
                    <td className="dim" style={{ whiteSpace: 'normal' }}>
                      {a.detail ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </State>
      </div>
    </>
  );
}
