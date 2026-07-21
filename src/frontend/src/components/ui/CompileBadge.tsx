import type { CSSProperties } from 'react';
import { Icon } from './Icon';
import { fmtFullTime, fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';

/**
 * 编译徽章：AI 介绍由哪个模型编译 + 编译时间（"model · MM-DD HH:mm"）。
 * 模型缺失（存量数据）时只显示时间；两者都没有（未编译）不渲染。
 */
export function CompileBadge({
  model,
  at,
  style,
}: {
  model?: string | null;
  at?: string | null;
  style?: CSSProperties;
}) {
  if (!model && !at) return null;
  const time = at ? fmtTime(at) : null;
  const title = model
    ? at
      ? tr(`由 ${model} 编译于 ${fmtFullTime(at)}`, `Compiled by ${model} at ${fmtFullTime(at)}`)
      : tr(`由 ${model} 编译`, `Compiled by ${model}`)
    : tr(`编译于 ${fmtFullTime(at)}`, `Compiled at ${fmtFullTime(at)}`);
  return (
    <span
      className="pill sm mono"
      title={title}
      style={{ background: 'var(--surface-3)', color: 'var(--text-3)', fontWeight: 500, ...style }}
    >
      <Icon name="cpu" size={11} />
      {[model, time].filter(Boolean).join(' · ')}
    </span>
  );
}
