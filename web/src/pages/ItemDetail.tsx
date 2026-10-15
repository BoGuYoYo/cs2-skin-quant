import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../lib/api';
import { useApi } from '../lib/useApi';
import { KLine } from '../components/KLine';
import { QuantileBand } from '../components/QuantileBand';
import { State } from '../components/State';
import {
  dateTime,
  int,
  levelTone,
  logPct,
  money,
  num,
  pct,
  relTime,
  rarityTone,
  sideLabel,
  windowFit,
} from '../lib/format';

/**
 * unit 语义（必须与后端量纲对齐，否则会出现 3000% 这种错值）：
 *   'num'    裸数值（已经是百分数的字段也用它）
 *   'pct'    普通比率，如 0.18 → 18%
 *   'logpct' 对数收益，需要 expm1 还原，如 0.182 → 20%
 *
 * 踩过的坑：后端 list_slope_7d 已经是 pct_change(7)*100、supply_growth_30d
 * 也已经是百分数，前端再乘一次 100 就变成 3000%；而 ret_7d/21d/30d 与
 * rel_ret_21d 是对数收益，直接用 pct() 会把「跌 50%」显示成「-69.3%」。
 */
function FactorRow({
  label,
  value,
  unit = 'num',
  hint,
  scaleMax,
}: {
  label: string;
  value: number | null;
  unit?: 'num' | 'pct' | 'logpct' | 'pctnum';
  hint?: string;
  scaleMax?: number;
}) {
  const shown = unit === 'pct' ? pct(value, 2)
    : unit === 'logpct' ? logPct(value, 2)
      : unit === 'pctnum' ? (value === null ? '—' : `${value.toFixed(2)}%`)
        : num(value, 2);
  const scale = unit === 'pct' ? 100
    : unit === 'logpct' ? 100
      : unit === 'pctnum' ? 1
        : scaleMax ?? 20;
  return (
    <div className="factor-row">
      <span className="lab">{label}</span>
      <span className="bar">
        <i style={{ width: `${Math.min(100, Math.abs((value ?? 0) * scale))}%` }} />
      </span>
      <span className="val">{shown}</span>
      {hint ? <span className="dim" style={{ gridColumn: '1 / -1', fontSize: 11 }}>{hint}</span> : null}
    </div>
  );
}

export default function ItemDetail() {
  const { id = '' } = useParams();
  const [days, setDays] = useState(180);

  const detail = useApi(() => api.item(id), [id]);
  const kl = useApi(() => api.kline(id, days), [id, days]);

  const it = detail.data?.item;
  const f = detail.data?.factor;
  const fc = detail.data?.forecast;
  const cm = detail.data?.caseMeta;
  const side = sideLabel(fc?.side);
  const fit = windowFit(f?.drop_duration_days);

  return (
    <>
      <div className="ark-pageHead">
        <div style={{ display: 'flex', gap: 14, alignItems: 'center' }}>
          {it?.image ? (
            <img src={it.image} alt="" style={{ width: 96, height: 66, objectFit: 'contain', background: 'var(--bg-3)', borderRadius: 8 }} />
          ) : null}
          <div>
            <h1>{it?.name ?? id}</h1>
            <div className="sub mono">
              {it?.market_hash_name ?? '—'}
              {it?.wear ? ` · ${it.wear}` : ''}
            </div>
            <div className="ark-pageActions" style={{ marginTop: 8 }}>
              {it?.case_name ? <span className="tag t-mute">{it.case_name}</span> : null}
              {fc?.stale ? (
                // 「最近一次运行没有产生有效截面」时，这条是历史预测 ——
                // 不能把 side=buy 当成当前建议挂在这里（后端已标注 stale）
                <span className="tag t-warn">历史预测 · 非当前建议</span>
              ) : (
                <span className={`tag t-${side.tone}`}>{side.text}</span>
              )}
              <span className={`tag t-${fit.tone}`}>下跌 {f?.drop_duration_days ?? '—'} 天 · {fit.text}</span>
            </div>
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div className="mono" style={{ fontSize: 26, fontWeight: 680 }}>
            {money(f?.price)}
          </div>
          <div className="dim" style={{ fontSize: 12 }}>
            30日回撤 <span className="neg">{pct(f?.dd_from_high, 1)}</span>
            {' · '}
            相对大盘 21日 {logPct(f?.rel_ret_21d, 1)}
          </div>
        </div>
      </div>

      <div className="grid g2" style={{ marginBottom: 14 }}>
        <div className="ark-panel">
          <h3>预测区间（未来 21 日，扣费后对数净收益）</h3>
          <State loading={detail.loading} error={detail.error}>
            {fc?.stale ? (
              <div className="t-warn" style={{ fontSize: 12, marginBottom: 8 }}>
                ⚠️ {fc.staleReason ?? '这是历史预测'}。下面的数值不是当前建议。
              </div>
            ) : null}
            <QuantileBand q10={fc?.q10_adj ?? null} q50={fc?.q50 ?? null} q90={fc?.q90_adj ?? null} />
            <div className="grid g3" style={{ marginTop: 14 }}>
              <div>
                <div className="dim" style={{ fontSize: 12 }}>反弹≥10% 概率（模型估计）</div>
                <div className="mono" style={{ fontSize: 18 }}>{fc?.prob_up === null || fc?.prob_up === undefined ? '—' : pct(fc.prob_up, 0)}</div>
              </div>
              <div>
                <div className="dim" style={{ fontSize: 12 }}>综合打分</div>
                <div className="mono" style={{ fontSize: 18 }}>{num(fc?.score, 1)}</div>
              </div>
              <div>
                <div className="dim" style={{ fontSize: 12 }}>建议仓位</div>
                <div className="mono" style={{ fontSize: 18 }}>{fc?.size ? pct(fc.size, 2) : '—'}</div>
              </div>
            </div>
            {fc?.why ? <div className="dim" style={{ fontSize: 12, marginTop: 10 }}>{fc.why}</div> : null}
          </State>
        </div>

        <div className="ark-panel">
          <h3>供给面（你的核心判断依据）</h3>
          <div className="grid g2">
            <div>
              <div className="dim" style={{ fontSize: 12 }}>箱子稀有度</div>
              <div className={`mono tag t-${rarityTone(cm?.rarity_score ?? f?.case_rarity_score)}`} style={{ fontSize: 14 }}>
                {num(cm?.rarity_score ?? f?.case_rarity_score, 0)} / 100
              </div>
            </div>
            <div>
              <div className="dim" style={{ fontSize: 12 }}>箱子日成交（≈开箱量）</div>
              <div className="mono" style={{ fontSize: 14 }}>{int(cm?.case_volume_30d)}</div>
            </div>
            <div>
              <div className="dim" style={{ fontSize: 12 }}>存世量 30 日增速</div>
              <div className="mono" style={{ fontSize: 14 }}>{num(cm?.supply_growth_30d, 2)}</div>
            </div>
            <div>
              <div className="dim" style={{ fontSize: 12 }}>掉落池</div>
              <div className="mono" style={{ fontSize: 14 }}>{cm?.drop_pool ?? '—'}</div>
            </div>
          </div>
          <div style={{ marginTop: 12 }}>
            <FactorRow label="存世量同箱分位" value={f?.supply_peer_pct ?? null} unit="pct" />
            <FactorRow label="供给刚性（合成）" value={f?.supply_rigidity ?? null} scaleMax={100} />
            <FactorRow label="换手率" value={f?.turnover ?? null} unit="pct" />
            <FactorRow label="在售 / 存世" value={f?.list_ratio ?? null} unit="pct" />
            <FactorRow label="挂单量 7 日变化" value={f?.list_slope_7d ?? null} unit="pctnum" />
          </div>
          {cm?.rationale ? (
            <div className="ark-note" style={{ marginTop: 12, marginBottom: 0 }}>
              <b>LLM 评分理由：</b>
              {cm.rationale}
            </div>
          ) : null}
          {cm?.blocking_risk ? (
            <div className="ark-note" style={{ marginTop: 8, marginBottom: 0, borderColor: 'rgba(255,92,92,0.25)', background: 'rgba(255,92,92,0.06)' }}>
              <b>主要风险：</b>
              {cm.blocking_risk}
            </div>
          ) : null}
        </div>
      </div>

      <div className="ark-panel" style={{ marginBottom: 14 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>K 线（底价曲线）</h3>
          <div className="ark-pageActions">
            {[30, 90, 180, 365].map((d) => (
              <button key={d} className={days === d ? 'ark-btn is-primary' : 'ark-btn'} onClick={() => setDays(d)}>
                {d}D
              </button>
            ))}
          </div>
        </div>
        <State loading={kl.loading} error={kl.error} empty={(kl.data ?? []).length === 0}>
          <KLine data={kl.data ?? []} height={400} />
        </State>
      </div>

      <div className="grid g2">
        <div className="ark-panel">
          <h3>动量与波动</h3>
          <FactorRow label="7 日收益" value={f?.ret_7d ?? null} unit="logpct" />
          <FactorRow label="21 日收益" value={f?.ret_21d ?? null} unit="logpct" />
          <FactorRow label="30 日收益" value={f?.ret_30d ?? null} unit="logpct" />
          <FactorRow label="21 日波动率" value={f?.vol_21 ?? null} unit="pct" />
          <div className="grid g2" style={{ marginTop: 12 }}>
            <div>
              <div className="dim" style={{ fontSize: 12 }}>看空事件（30日）</div>
              <div className={`mono ${f?.event_bearish_30d == null ? 'dim' : 'neg'}`} style={{ fontSize: 16 }}>
                {int(f?.event_bearish_30d)}
              </div>
            </div>
            <div>
              <div className="dim" style={{ fontSize: 12 }}>看多事件（30日）</div>
              <div className={`mono ${f?.event_bullish_30d == null ? 'dim' : 'pos'}`} style={{ fontSize: 16 }}>
                {int(f?.event_bullish_30d)}
              </div>
            </div>
          </div>
        </div>

        <div className="ark-panel">
          <h3>历史异动</h3>
          <State loading={detail.loading} error={detail.error} empty={(detail.data?.alerts ?? []).length === 0}
                 emptyText="没有触发过告警">
            <div className="ark-tableWrap" style={{ border: 'none' }}>
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>等级</th>
                    <th className="num">打分</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.data?.alerts ?? []).map((a) => (
                    <tr key={`${a.level}-${a.ts}`}>
                      <td className="dim" title={dateTime(a.ts)}>{relTime(a.ts)}</td>
                      <td><span className={`tag t-${levelTone(a.level)}`}>{a.level}</span></td>
                      <td className="num">{a.score?.toFixed(0)}</td>
                      <td className="dim" style={{ whiteSpace: 'normal' }}>{a.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </State>
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <Link className="tag t-mute" to="/candidates">← 返回候选池</Link>
        {fc?.date ? <span className="dim mono" style={{ marginLeft: 10 }}>预测日 {fc.date}</span> : null}
        {fc?.horizon ? <span className="dim mono" style={{ marginLeft: 10 }}>窗口 {fc.horizon}D</span> : null}
        {fc?.model_version ? <span className="dim mono" style={{ marginLeft: 10 }}>{fc.model_version}</span> : null}
      </div>
    </>
  );
}
