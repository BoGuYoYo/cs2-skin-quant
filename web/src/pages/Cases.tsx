import { useState } from 'react';
import { api } from '../lib/api';
import { useApi } from '../lib/useApi';
import { State } from '../components/State';
import { int, money, num, pct, rarityTone } from '../lib/format';

export default function Cases() {
  const [sort, setSort] = useState('rarity');
  const [limit, setLimit] = useState(100);
  const q = useApi(() => api.cases({ sort, limit }), [sort, limit]);

  return (
    <>
      <div className="ark-pageHead">
        <div>
          <h1>箱子稀有度</h1>
          <div className="ark-sub">
            这是整套判断的支点：箱子稀有 → 新增供给趋近于零 → 下跌是存量博弈而非价值毁灭
          </div>
        </div>
        <div className="ark-pageActions">
          <button className={sort === 'rarity' ? 'ark-btn is-primary' : 'ark-btn'} onClick={() => setSort('rarity')}>
            按稀有度
          </button>
          <button className={sort === 'volume' ? 'ark-btn is-primary' : 'ark-btn'} onClick={() => setSort('volume')}>
            按开箱量
          </button>
          <button className="ark-btn" onClick={() => q.reload()}>刷新</button>
        </div>
      </div>

      <div className="ark-note">
        <b>箱子日成交量</b> ≈ 开箱量 ≈ 该箱物品的新增供给速率。「存世量 30 日增速」趋近 0
        说明供给已经饱和，箱子里的东西不再被源源不断地生产出来——这才是「稀缺」的可验证形式。
      </div>

      <State loading={q.loading} error={q.error} empty={(q.data ?? []).length === 0}
             emptyText="还没有箱子数据（管线未推送 case_meta）">
        <div className="ark-tableWrap">
          <table>
            <thead>
              <tr>
                <th>武器箱</th>
                <th className="num">稀有度</th>
                <th className="num">箱子价</th>
                <th className="num">日成交（开箱量）</th>
                <th className="num">存世量 30 日增速</th>
                <th>掉落池</th>
                <th>主要风险</th>
              </tr>
            </thead>
            <tbody>
              {(q.data ?? []).map((c) => (
                <tr key={c.case_id}>
                  <td>
                    <span className="nm">{c.case_name}</span>
                    {c.rationale ? (
                      <div className="cs" style={{ whiteSpace: 'normal', maxWidth: 380 }}>{c.rationale}</div>
                    ) : null}
                  </td>
                  <td className="num">
                    <span className={`tag t-${rarityTone(c.rarity_score)}`}>{num(c.rarity_score, 0)}</span>
                  </td>
                  <td className="num">{money(c.case_price)}</td>
                  <td className="num">{int(c.case_volume_30d)}</td>
                  <td className="num">{num(c.supply_growth_30d, 2)}</td>
                  <td>
                    <span className={`tag ${c.drop_pool === 'removed' ? 't-good' : c.drop_pool === 'active' ? 't-warn' : 't-mute'}`}>
                      {c.drop_pool ?? '—'}
                    </span>
                  </td>
                  <td className="dim" style={{ whiteSpace: 'normal', maxWidth: 300 }}>{c.blocking_risk ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </State>
    </>
  );
}
