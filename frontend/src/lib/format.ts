/* 时间/时长格式化小工具。 */

import { tr } from './i18n';

/** ISO 时间 → "MM-DD HH:mm"（本地时区）。无效输入返回 '—'。 */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** ISO 时间 → "YYYY-MM-DD HH:mm:ss"（本地时区），用于 hover 完整时间提示。 */
export function fmtFullTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * ISO 时间 → 相对时间大白话：
 * "刚刚" / "8 分钟前" / "3 小时前" / "昨天 21:28" / "5 天前"；
 * 超过 7 天回退到日期（跨年时带年份）。无效输入返回 '—'。
 */
export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 60_000) return tr('刚刚', 'just now');
  const min = Math.floor(diff / 60_000);
  if (min < 60) return tr(`${min} 分钟前`, `${min} min ago`);

  const p = (n: number) => String(n).padStart(2, '0');
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(d)) / 86_400_000);

  const hours = Math.floor(min / 60);
  if (dayDiff <= 0) return tr(`${hours} 小时前`, `${hours} h ago`);
  if (dayDiff === 1) return tr(`昨天 ${p(d.getHours())}:${p(d.getMinutes())}`, `yesterday ${p(d.getHours())}:${p(d.getMinutes())}`);
  if (dayDiff <= 7) return tr(`${dayDiff} 天前`, `${dayDiff} days ago`);
  if (d.getFullYear() === now.getFullYear()) return `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 两个 ISO 时间之间的时长 → "42s" / "3m 12s" / "1h 04m"。 */
export function fmtDuration(startIso: string | null | undefined, endIso?: string | null): string {
  if (!startIso) return '—';
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '—';
  const s = Math.round((end - start) / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, '0')}m`;
}

/** token 数缩写：1234 → "1.2k"。 */
export function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}
