import { describe, expect, it } from 'vitest';

import { parseConfigDraft, parseTreeFile, pluginBadge, validationErrorLines } from '../pluginsView';

/* ============================================================
   设置页「插件」tab 的纯函数（#707）：
   ① 状态徽章映射——四种运行态各有其色，error 把详情带进 title；
   ② 配置文本域解析——语法错/非对象在前端就拦下，不去打 IPC；
   ③ 校验错误渲染——path 在前逐行拼；
   ④ 导入文件形状检查——version/entries 不对就直接拒。
   ============================================================ */

describe('pluginBadge', () => {
  it('四种状态映射到不同的文案与配色', () => {
    expect(pluginBadge({ state: 'active' })).toMatchObject({ zh: '运行中', bg: 'var(--ok-bg)' });
    expect(pluginBadge({ state: 'disabled' })).toMatchObject({ zh: '已停用', bg: 'var(--surface-3)' });
    expect(pluginBadge({ state: 'pending' })).toMatchObject({ zh: '启动中', bg: 'var(--warn-bg)' });
    expect(pluginBadge({ state: 'error' })).toMatchObject({ zh: '出错', bg: 'var(--danger-bg)' });
  });

  it('出错时错误详情进 title，供悬停查看', () => {
    expect(pluginBadge({ state: 'error', error: 'boom' }).title).toBe('boom');
    expect(pluginBadge({ state: 'error' }).title).toBeUndefined();
    expect(pluginBadge({ state: 'active', error: 'stale' }).title).toBeUndefined();
  });
});

describe('parseConfigDraft', () => {
  it('空文本视作空配置，合法对象原样返回', () => {
    expect(parseConfigDraft('   ')).toEqual({ ok: true, config: {} });
    expect(parseConfigDraft('{"a": 1}')).toEqual({ ok: true, config: { a: 1 } });
  });

  it('语法错与非对象（数组/标量/null）都在前端拦下', () => {
    expect(parseConfigDraft('{oops').ok).toBe(false);
    expect(parseConfigDraft('[1, 2]').ok).toBe(false);
    expect(parseConfigDraft('"str"').ok).toBe(false);
    expect(parseConfigDraft('null').ok).toBe(false);
  });
});

describe('validationErrorLines', () => {
  it('path 在前拼一行；没有 path 只剩 message', () => {
    expect(
      validationErrorLines([
        { path: 'timeout', message: 'must be a number' },
        { path: '', message: 'unknown key "foo"' },
      ]),
    ).toEqual(['timeout: must be a number', 'unknown key "foo"']);
  });
});

describe('parseTreeFile', () => {
  it('只接受 version=1 且 entries 为数组的载荷', () => {
    expect(parseTreeFile('{"version": 1, "entries": []}').ok).toBe(true);
    expect(parseTreeFile('{"version": 2, "entries": []}').ok).toBe(false);
    expect(parseTreeFile('{"entries": []}').ok).toBe(false);
    expect(parseTreeFile('{"version": 1}').ok).toBe(false);
    expect(parseTreeFile('not json').ok).toBe(false);
  });
});
