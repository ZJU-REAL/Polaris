import { describe, expect, it } from 'vitest';

import {
  installPhaseText,
  marketEntryState,
  marketKindLabel,
  marketPermissionLabels,
  marketTierBadge,
  normalizePackageId,
} from '../marketView';

/* ============================================================
   设置页插件市场分区的纯函数（#711）：
   ① 安装态机——未装 → 安装中 → 已装未启 → 已启用，装/启分离的中间态
      必须显式存在，且安装进行中优先于旧条目的「已装」；
   ② 徽章/权限/阶段文案映射——未知值原样兜底，权限空声明也要说清。
   ============================================================ */

const plugins = (
  ...rows: { id: string; name: string; disabled: boolean }[]
): { id: string; name: string; disabled: boolean }[] => rows;

// 市场安装登记的树条目真实形状：id=规范化包名，name=file:// 入口 URL
const FILE_URL = 'file:///plugins/a/1.0.0/index.mjs';

describe('marketEntryState 安装态机', () => {
  it('列表里没有对应条目、也没在装 → 未安装', () => {
    expect(marketEntryState('a', plugins(), {})).toEqual({ kind: 'not-installed' });
    expect(marketEntryState('a', plugins({ id: 'b', name: FILE_URL, disabled: true }), {})).toEqual({
      kind: 'not-installed',
    });
  });

  it('有进行中的安装 → 安装中，且带进度；旧条目仍在列表时安装中优先', () => {
    const progress = { phase: 'extract', done: 3, total: 4 };
    expect(marketEntryState('a', plugins(), { a: progress })).toEqual({ kind: 'installing', progress });
    // 升级/重装：列表里已有旧条目，此刻用户关心进度而不是旧条目开关
    expect(marketEntryState('a', plugins({ id: 'a', name: FILE_URL, disabled: false }), { a: progress })).toEqual({
      kind: 'installing',
      progress,
    });
  });

  it('装完（disabled 条目出现、安装记录清掉）→ 已装未启，带插件 id 供就地启用', () => {
    expect(marketEntryState('a', plugins({ id: 'a', name: FILE_URL, disabled: true }), {})).toEqual({
      kind: 'installed-disabled',
      pluginId: 'a',
    });
  });

  it('启用后 → 已启用；命中多条时只要有一个没停用就算已启用', () => {
    expect(marketEntryState('a', plugins({ id: 'a', name: FILE_URL, disabled: false }), {})).toEqual({
      kind: 'installed-enabled',
      pluginId: 'a',
    });
    expect(
      marketEntryState(
        'a',
        plugins({ id: 'a', name: FILE_URL, disabled: true }, { id: 'a', name: FILE_URL, disabled: false }),
        {},
      ),
    ).toEqual({ kind: 'installed-enabled', pluginId: 'a' });
  });

  it('scoped 包按规范化 id 匹配：@scope/pkg → scope--pkg（与 kernel packageEntryId 同步）', () => {
    expect(normalizePackageId('@scope/pkg')).toBe('scope--pkg');
    expect(normalizePackageId('plain-pkg')).toBe('plain-pkg');
    expect(
      marketEntryState('@scope/pkg', plugins({ id: 'scope--pkg', name: FILE_URL, disabled: true }), {}),
    ).toEqual({ kind: 'installed-disabled', pluginId: 'scope--pkg' });
  });
});

describe('徽章与文案映射', () => {
  it('种类/分级已知值有大白话文案，未知值原样兜底不空白', () => {
    expect(marketKindLabel('datasource').zh).toBe('数据源');
    expect(marketKindLabel('something-new')).toEqual({ zh: 'something-new', en: 'something-new' });
    expect(marketTierBadge('platinum')).toMatchObject({ zh: '白金', bg: 'var(--accent-soft)' });
    expect(marketTierBadge('mystery').zh).toBe('mystery');
  });

  it('权限如实映射：声明什么列什么，什么都没声明也要明说', () => {
    expect(marketPermissionLabels({ network: true, filesystem: true }).map((p) => p.zh)).toEqual([
      '声明需要联网',
      '声明需要读写文件',
    ]);
    expect(marketPermissionLabels({}).map((p) => p.zh)).toEqual(['未声明任何权限']);
  });

  it('安装四阶段各有其文案，未知阶段原样展示', () => {
    expect(['download', 'verify', 'extract', 'register'].map((p) => installPhaseText(p).zh)).toEqual([
      '下载中',
      '校验中',
      '解压中',
      '登记中',
    ]);
    expect(installPhaseText('warmup').zh).toBe('warmup');
  });
});
