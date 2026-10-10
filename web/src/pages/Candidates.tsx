import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api';
import { useApi } from '../lib/useApi';
import { CandidateTable } from '../components/CandidateTable';
import { State } from '../components/State';
import { int, money, pct, num, rarityTone } from '../lib/format';

type Mode = 'pool' | 'screener';

export default function Candidates() {
  const [mode, setMode] = useState<Mode>('pool');
  const [sort, setSort] = useState('score');
  const [side, setSide] = useState('');
  const [limit, setLimit] = useState(100);

  // 经验法则默认值：≤100 元、换手 ≥0.2%、在售占比 ≤3.5%、箱稀有度 ≥60
  const [priceMax, setPriceMax] = useState(100);
  const [turnoverMin, setTurnoverMin] = useState(0.2);
  const [listRatioMax, setListRatioMax] = useState(3.5);
  const [rarityMin, setRarityMin] = useState(60);

  const pool = useApi(
    () => api.candidates({ sort, side, limit }),
    [sort, side, limit],
  );
  const scr = useApi(
    () => api.screener({ priceMax, turnoverMin: turnoverMin / 100, listRatioMax: listRatioMax / 100, rarityMin, limit }),
    [priceMax, turnoverMin, listRatioMax, rarityMin, limit],
  );

  return (
    <>
      <div className="topbar">
        <div>
          <h1>候选池</h1>
          <div className="sub">
            {mode === 'pool'
              ? `模型打分排序 · 基准日 ${pool.data?.date ?? '—'}`
              : '经验法则筛选：小件 + 相对稀缺 + 箱子稀有 + 抛压不重 + 有真实换手'}
          </div>
        </div>
        <div className="badge-row">
          <button className={mode === 'pool' ? 'primary' : 'ghost'} onClick={() => setMode('pool')}>
            模型候选
          </button>
          <button className={mode === 'screener' ? 'primary' : 'ghost'} onClick={() => setMode('screener')}>
            经验筛选
          </button>
        </div>
      </div>

      <div className="note">
        「下跌天数」落在 <b>14~30 天</b> 才是甜蜜区——这是你的时间先验。<b>P10</b> 是校准后的下沿：
        它若为负，说明模型认为存在实质亏损风险，无论 P50 多漂亮都不该建仓。
      </div>

      <div className="toolbar">
        {mode === 'pool' ? (
          <>
            <label>
              排序
              <select value={sort} onChange={(e) => setSort(e.target.value)}>
                <option value="score">综合打分</option>
                <option value="q10">校准后 P10</option>
                <option value="prob">反弹概率</option>
                <option value="duration">下跌天数</option>
                <option value="rarity">箱稀有度</option>
                <option value="turnover">换手率</option>
              </select>
            </label>
            <label>
              判断
              <select value={side} onChange={(e) => setSide(e.target.value)}>
                <option value="">全部</option>
                <option value="buy">候选</option>
                <option value="watch">观察</option>
                <option value="skip">排除</option>
              </select>
            </label>
          </>
        ) : (
          <>
            <label>
              价格上限
              <input type="number" value={priceMax} onChange={(e) => setPriceMax(+e.target.value)} />
            </label>
            <label>
              换手率 ≥ %
              <input type="number" step="0.05" value={turnoverMin} onChange={(e) => setTurnoverMin(+e.target.value)} />
            </label>
            <label>
              在售占比 ≤ %
              <input type="number" step="0.1" value={listRatioMax} onChange={(e) => setListRatioMax(+e.target.value)} />
            </label>
            <label>
              箱稀有度 ≥
              <input type="number" value={rarityMin} onChange={(e) => setRarityMin(+e.target.value)} />
            </label>
          </>
        )}
        <label>
          条数
          <select value={limit} onChange={(e) => setLimit(+e.target.value)}>
            {[50, 100, 200, 500].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </label>
        <button className="ghost" onClick={() => (mode === 'pool' ? pool.reload() : scr.reload())}>
          刷新
        </button>
      </div>

      {mode === 'pool' ? (
        <State loading={pool.loading} error={pool.error} empty={(pool.data?.rows ?? []).length === 0}
               emptyText="没有符合模型条件的标的">
          <CandidateTable
            rows={pool.data?.rows ?? []}
            sortKey={sort}
            onSort={(k) => setSort(k === 'price' ? 'q10' : k)}
          />
        </State>
      ) : (
        <State loading={scr.loading} error={scr.error} empty={(scr.data ?? []).length === 0}
               emptyText="当前筛选条件下没有标的——放宽箱稀有度或换手率试试">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>饰品</th>
                  <th className="num">现价</th>
                  <th className="num">30日回撤</th>
                  <th className="num">下跌天数</th>
                  <th className="num">21日收益</th>
                  <th className="num">换手率</th>
                  <th className="num">在售占比</th>
                  <th className="num">存世分位</th>
                  <th className="num">箱稀有度</th>
                  <th className="num">供给刚性</th>
                </tr>
              </thead>
              <tbody>
                {(scr.data ?? []).map((r) => (
                  <tr key={r.item_id}>
                    <td>
                      <Link to={`/item/${encodeURIComponent(r.item_id)}`} className="item-cell">
                        {r.image ? <img src={r.image} alt="" loading="lazy" /> : <img alt="" />}
                        <span>
                          <span className="nm">{r.name ?? r.item_id}</span>
                          {r.case_name ? <span className="cs"> · {r.case_name}</span> : null}
                        </span>
                      </Link>
                    </td>
                    <td className="num">{money(r.price)}</td>
                    <td className="num neg">{pct(r.dd_from_high, 1)}</td>
                    <td className="num">{r.drop_duration_days ?? '—'}</td>
                    <td className={`num ${(r.ret_21d ?? 0) < 0 ? 'neg' : 'pos'}`}>{pct(r.ret_21d, 1)}</td>
                    <td className="num">{pct(r.turnover, 2)}</td>
                    <td className="num">{pct(r.list_ratio, 2)}</td>
                    <td className="num">{pct(r.supply_peer_pct, 0)}</td>
                    <td className="num">
                      <span className={`tag t-${rarityTone(r.case_rarity_score)}`}>
                        {num(r.case_rarity_score, 0)}
                      </span>
                    </td>
                    <td className="num">{num(r.supply_rigidity, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </State>
      )}
    </>
  );
}
