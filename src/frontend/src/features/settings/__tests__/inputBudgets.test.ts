import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { LlmInputBudgetSpec } from '../../../lib/api';
import { budgetsPayload, effectiveBudget, parsePositiveInt, specsByStage } from '../inputBudgets';

/**
 * 模型路由表里的输入预算（#811）。
 *
 * 界面上显示的「生效值」必须与后端 InputBudget.effective 同一算法，否则用户看到的
 * 数字和实际送进模型的不一样；保存前的提示必须覆盖后端会拒收的每一种情况。
 */
const FULLTEXT: LlmInputBudgetSpec = {
  stage: 'librarian',
  key: 'fulltext_chars',
  default: 24_000,
  minimum: 4_000,
  maximum: 2_000_000,
  chars_per_window_token: 2,
};
const EXCERPT: LlmInputBudgetSpec = {
  stage: 'forge',
  key: 'excerpt_chars',
  default: 800,
  minimum: 200,
  maximum: 50_000,
  chars_per_window_token: 2,
};

describe('parsePositiveInt', () => {
  it('accepts plain and grouped digits', () => {
    expect(parsePositiveInt('60000')).toBe(60_000);
    expect(parsePositiveInt('60,000')).toBe(60_000);
    expect(parsePositiveInt(' 60_000 ')).toBe(60_000);
  });

  it('treats empty, zero and junk as not set', () => {
    for (const raw of ['', '0', '-5', '1.5', '12k', 'abc']) expect(parsePositiveInt(raw)).toBeNull();
  });
});

describe('effectiveBudget', () => {
  it('shows the default when nothing is typed', () => {
    expect(effectiveBudget(FULLTEXT, '', null)).toEqual({ value: 24_000, cappedByWindow: false, problem: null });
  });

  it('shows the typed value when it is valid', () => {
    expect(effectiveBudget(FULLTEXT, '60000', 200_000).value).toBe(60_000);
  });

  it('says when the context window lowers the default', () => {
    const got = effectiveBudget(FULLTEXT, '', 8_000);
    expect(got.value).toBe(16_000);
    expect(got.cappedByWindow).toBe(true);
    expect(got.problem).toBeNull();
  });

  it('flags a typed value the server would refuse for its range', () => {
    expect(effectiveBudget(FULLTEXT, '100', null).problem).toEqual({ kind: 'range', min: 4_000, max: 2_000_000 });
  });

  it('flags a typed value larger than the window allows', () => {
    expect(effectiveBudget(FULLTEXT, '100000', 16_000).problem).toEqual({ kind: 'window', max: 32_000 });
  });
});

describe('budgetsPayload', () => {
  it('sends only filled keys that the stage registers', () => {
    expect(budgetsPayload({ fulltext_chars: '60,000', stray: '5' }, [FULLTEXT])).toEqual({ fulltext_chars: 60_000 });
  });

  it('sends nothing when every field is empty', () => {
    expect(budgetsPayload({ fulltext_chars: '' }, [FULLTEXT])).toBeUndefined();
  });
});

describe('specsByStage', () => {
  it('groups specs under their stage', () => {
    expect(Object.keys(specsByStage([FULLTEXT, EXCERPT])).sort()).toEqual(['forge', 'librarian']);
  });
});

describe('routes keep their window and budgets on save', () => {
  it('sends context_window and input_budgets with every route', () => {
    // PUT 是整表覆盖：保存时漏带哪个字段，那个字段就会被清空——context_window 以前就是这么丢的
    const page = readFileSync(join(__dirname, '..', 'SettingsPage.tsx'), 'utf8');
    expect(page).toContain('context_window: window');
    expect(page).toContain('input_budgets: budgets');
    expect(page).toContain("context_window: r.context_window ? String(r.context_window) : ''");
  });
});
