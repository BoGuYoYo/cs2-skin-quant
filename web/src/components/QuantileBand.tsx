import { logPct } from '../lib/format';

interface Props {
  q10: number | null;
  q50: number | null;
  q90: number | null;
  /** 成本线（扣费后要打平所需收益），默认 0 表示只对比盈亏平衡 */
  breakeven?: number;
}

/**
 * 分位数区间条：整套系统的核心可视化。
 * 下沿（P10）决定「要不要碰」——它落在 0 左边就说明模型认为有实质亏损风险。
 */
export function QuantileBand({ q10, q50, q90, breakeven = 0 }: Props) {
  if (q10 === null || q50 === null || q90 === null) {
    return <div className="dim mono">分位数缺失</div>;
  }
  const lo = Math.min(q10, -0.05);
  const hi = Math.max(q90, 0.05);
  const span = hi - lo || 1;
  const pos = (v: number) => ((v - lo) / span) * 100;

  const rangeLeft = pos(q10);
  const rangeWidth = Math.max(pos(q90) - pos(q10), 0.8);
  const zeroLeft = pos(breakeven);
  const midLeft = pos(q50);

  return (
    <div>
      <div className="band">
        <div className="range" style={{ left: `${rangeLeft}%`, width: `${rangeWidth}%` }} />
        <div className="mid" style={{ left: `${midLeft}%` }} />
        <div className="zero" style={{ left: `${zeroLeft}%` }} />
      </div>
      <div className="band-scale">
        <span>P10 {logPct(q10)}</span>
        <span>P50 {logPct(q50)}</span>
        <span>P90 {logPct(q90)}</span>
      </div>
    </div>
  );
}
