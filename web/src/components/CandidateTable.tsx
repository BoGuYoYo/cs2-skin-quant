import { Link } from 'react-router-dom';
import type { Candidate } from '../lib/api';
import { int, logPct, money, num, pct, rarityTone, sideLabel, windowFit } from '../lib/format';

export function ItemCell({
  id,
  name,
  image,
  caseName,
}: {
  id: string;
  name: string | null;
  image?: string | null;
  caseName?: string | null;
}) {
  return (
    <Link to={`/item/${encodeURIComponent(id)}`} className="item-cell">
      {image ? <img src={image} alt="" loading="lazy" /> : null}
      <span>
        <span className="nm">{name ?? id}</span>
        {caseName ? <span className="cs"> · {caseName}</span> : null}
      </span>
    </Link>
  );
}

interface Props {
  rows: Candidate[];
  compact?: boolean;
  sortKey?: string;
  sortDir?: 'asc' | 'desc';
  onSort?: (key: string) => void;
}

export function CandidateTable({ rows, compact = false, sortKey, sortDir = 'desc', onSort }: Props) {
  const th = (key: string, label: string, cls = '') => (
    <th
      className={`${cls} ${onSort ? 'sortable' : ''}`}
      onClick={onSort ? () => onSort(key) : undefined}
    >
      {label}
      {sortKey === key ? (sortDir === 'asc' ? ' ▴' : ' ▾') : ''}
    </th>
  );

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>饰品</th>
            {th('price', '现价', 'num')}
            {th('duration', '下跌天数', 'num')}
            {th('rarity', '箱稀有度', 'num')}
            {th('turnover', '换手率', 'num')}
            {th('q10', 'P10', 'num')}
            <th className="num">P50</th>
            <th className="num">P90</th>
            {th('score', '打分', 'num')}
            <th className="num">反弹≥10%</th>
            <th className="num">仓位</th>
            <th>判断</th>
            {!compact && <th>依据</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const side = sideLabel(r.side);
            const fit = windowFit(r.drop_duration_days);
            const rt = rarityTone(r.case_rarity_score);
            const p10Tone = r.q10_adj === null ? 'dim' : r.q10_adj <= 0 ? 'neg' : 'pos';
            return (
              <tr key={r.item_id}>
                <td>
                  <ItemCell id={r.item_id} name={r.name} image={r.image} caseName={r.case_name} />
                </td>
                <td className="num">{money(r.price)}</td>
                <td className="num">
                  {r.drop_duration_days ?? '—'} <span className={`tag t-${fit.tone}`}>{fit.text}</span>
                </td>
                <td className="num">
                  <span className={`tag t-${rt}`}>{num(r.case_rarity_score, 0)}</span>
                </td>
                <td className="num">{pct(r.turnover, 2)}</td>
                <td className={`num ${p10Tone}`} title={r.q10_adj === null ? '校准后 P10 缺失' : undefined}>
                  {logPct(r.q10_adj)}
                </td>
                <td className="num">{logPct(r.q50)}</td>
                <td className="num dim" title={r.q90_adj === null ? '校准后 P90 缺失' : undefined}>
                  {logPct(r.q90_adj)}
                </td>
                <td className="num">{num(r.score, 1)}</td>
                <td className="num">{r.prob_up === null ? '—' : pct(r.prob_up, 0)}</td>
                <td className="num">{r.size ? pct(r.size, 2) : '—'}</td>
                <td>
                  <span className={`tag t-${side.tone}`}>{side.text}</span>
                </td>
                {!compact && <td className="dim" style={{ whiteSpace: 'normal', maxWidth: 320 }}>{r.why ?? '—'}</td>}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
