import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api';
import { useApi } from '../lib/useApi';
import { State } from '../components/State';
import { dateTime, levelTone, relTime } from '../lib/format';

export default function Alerts() {
  const [level, setLevel] = useState('');
  const [limit, setLimit] = useState(200);
  const q = useApi(() => api.alerts({ level, limit }), [level, limit]);

  const rows = q.data ?? [];
  const counts = rows.reduce<Record<string, number>>((acc, r) => {
    acc[r.level] = (acc[r.level] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <>
      <div className="ark-pageHead">
        <div>
          <h1>异动提醒</h1>
          <div className="ark-sub">
            分级逻辑：L1 观察（-8%）· L2 注意（-15%）· L3 强警（-25%）
            {Object.keys(counts).length > 0 && <> · 本页 L3 {counts.L3 ?? 0} 条</>}
          </div>
        </div>
        <div className="ark-pageActions">
          {['', 'L3', 'L2', 'L1'].map((lv) => (
            <button key={lv || 'all'} className={level === lv ? 'ark-btn is-primary' : 'ark-btn'} onClick={() => setLevel(lv)}>
              {lv || '全部'}
            </button>
          ))}
          <select value={limit} onChange={(e) => setLimit(+e.target.value)}>
            {[100, 200, 500].map((n) => (
              <option key={n} value={n}>{n} 条</option>
            ))}
          </select>
          <button className="ark-btn" onClick={() => q.reload()}>刷新</button>
        </div>
      </div>

      <div className="ark-note">
        手机提醒按冷却期去重（同品同级 2~12 小时只发一次）。当天看板会随最新一轮运行刷新，
        历史日期保留；「已推送」表示手机通知已经成功送出。
      </div>

      <State loading={q.loading} error={q.error} empty={rows.length === 0} emptyText="还没有触发过告警">
        <div className="ark-tableWrap">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>等级</th>
                <th>饰品</th>
                <th>所属箱子</th>
                <th className="num">打分</th>
                <th>说明</th>
                <th className="num">已推送</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id}>
                  <td className="dim" title={dateTime(a.ts)}>{relTime(a.ts)}</td>
                  <td><span className={`tag t-${levelTone(a.level)}`}>{a.level}</span></td>
                  <td>
                    <Link to={`/item/${encodeURIComponent(a.item_id)}`} className="item-cell">
                      {a.image ? <img src={a.image} alt="" loading="lazy" /> : null}
                      <span className="nm">{a.name ?? a.item_id}</span>
                    </Link>
                  </td>
                  <td className="dim">{a.case_name ?? '—'}</td>
                  <td className="num">{a.score?.toFixed(0) ?? '—'}</td>
                  <td className="dim" style={{ whiteSpace: 'normal', maxWidth: 460 }}>{a.detail ?? '—'}</td>
                  <td className="num">{a.pushed ? '✓' : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </State>
    </>
  );
}
