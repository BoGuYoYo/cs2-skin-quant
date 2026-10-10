/** 展示层格式化：分位数是「对数净收益」，展示时必须换回百分比 */

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

/** 对数收益 → 百分比（log1p 的逆） */
export function logPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${(Math.expm1(v) * 100).toFixed(digits)}%`;
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return v.toFixed(digits);
}

export function money(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `¥${v.toFixed(digits)}`;
}

export function int(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Math.round(v).toLocaleString('zh-CN');
}

export function dateTime(ts: number | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', { hour12: false });
}

export function relTime(ts: number | null | undefined): string {
  if (!ts) return '—';
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

export function sideLabel(side: string | null | undefined): { text: string; tone: string } {
  switch (side) {
    case 'buy':
      return { text: '候选', tone: 'good' };
    case 'watch':
      return { text: '观察', tone: 'warn' };
    case 'skip':
      return { text: '排除', tone: 'bad' };
    default:
      return { text: '—', tone: 'mute' };
  }
}

export function levelTone(level: string): string {
  return level === 'L3' ? 'bad' : level === 'L2' ? 'warn' : 'mute';
}

/** 「下跌 14~30 天」经验先验的匹配度 */
export function windowFit(days: number | null | undefined): { text: string; tone: string } {
  if (days === null || days === undefined) return { text: '—', tone: 'mute' };
  if (days >= 14 && days <= 30) return { text: '甜蜜区', tone: 'good' };
  if (days >= 7 && days < 14) return { text: '偏早', tone: 'warn' };
  if (days > 30) return { text: '超窗口', tone: 'warn' };
  return { text: '刚起跌', tone: 'mute' };
}

export function rarityTone(score: number | null | undefined): string {
  if (score === null || score === undefined) return 'mute';
  if (score >= 75) return 'good';
  if (score >= 60) return 'warn';
  return 'mute';
}
