import { describe, expect, it } from 'vitest';

import {
  COMPARISON_FIELD_META,
  comparisonColumnTitle,
  comparisonToCsv,
  csvEscape,
  type ComparisonCell,
  type ComparisonTable,
} from '../comparison';

/* ============================================================
   对比表 CSV 拼装（#669）：纯函数，重点测三件事——
   ① absent cell 落调用方给的占位文案（不是空白也不是 "null"）；
   ② list 字段后端已拼成串，CSV 原样透传；
   ③ RFC 4180 转义（逗号 / 引号 / 换行）不破列。
   顺序稳定（行 = rows 原序，列 = papers 原序）是导出可复算的前提。
   ============================================================ */

function cell(value: string | null, extra: Partial<ComparisonCell> = {}): ComparisonCell {
  return {
    value,
    present: value != null,
    schema_id: 'skeleton',
    extracted_at: value != null ? '2026-09-01T00:00:00Z' : null,
    ...extra,
  };
}

const TABLE: ComparisonTable = {
  papers: [
    { paper_id: 'p1', title: 'Paper One', year: 2026 },
    { paper_id: 'p2', title: 'Paper, "Two"', year: null },
  ],
  rows: [
    { field: 'problem', schema_id: 'skeleton', cells: [cell('问题 A'), cell(null)] },
    // list 字段：后端已用中文分号拼串，前端原样进 CSV
    { field: 'findings', schema_id: 'skeleton', cells: [cell('发现一；发现二'), cell(null)] },
    {
      field: 'protocol',
      schema_id: 'method',
      cells: [cell('步骤1, 然后 "验证"\n换行继续'), cell(null)],
    },
  ],
};

describe('csvEscape', () => {
  it('leaves plain cells untouched', () => {
    expect(csvEscape('问题 A')).toBe('问题 A');
  });

  it('quotes commas, quotes and newlines; doubles inner quotes', () => {
    expect(csvEscape('a,b')).toBe('"a,b"');
    expect(csvEscape('say "hi"')).toBe('"say ""hi"""');
    expect(csvEscape('line1\nline2')).toBe('"line1\nline2"');
  });
});

describe('comparisonColumnTitle', () => {
  it('appends year only when present', () => {
    expect(comparisonColumnTitle(TABLE.papers[0]!)).toBe('Paper One (2026)');
    expect(comparisonColumnTitle(TABLE.papers[1]!)).toBe('Paper, "Two"');
  });
});

describe('comparisonToCsv', () => {
  it('keeps row/column order, fills absent cells with the given text', () => {
    const csv = comparisonToCsv(TABLE, {
      fieldLabel: (f) => COMPARISON_FIELD_META[f]?.zh ?? f,
      fieldColumnTitle: '字段',
      absentText: '未抽取',
    });
    const lines = csv.split('\r\n');
    // 表头 + 3 行 + 末尾换行产生的空串
    expect(lines).toHaveLength(5);
    expect(lines[0]).toBe('字段,Paper One (2026),"Paper, ""Two"""');
    expect(lines[1]).toBe('研究问题,问题 A,未抽取');
    expect(lines[2]).toBe('主要发现,发现一；发现二,未抽取');
    // 含逗号/引号/换行的 cell 整体加引号、内部引号翻倍；cell 内的 \n 不是行分隔符
    // （\r\n 才是），split 后整行仍是一条
    expect(lines[3]).toBe('实验流程,"步骤1, 然后 ""验证""\n换行继续",未抽取');
    expect(lines[4]).toBe('');
  });

  it('defaults: raw field names, empty absent cells', () => {
    const csv = comparisonToCsv(TABLE);
    const lines = csv.split('\r\n');
    expect(lines[0]).toBe('field,Paper One (2026),"Paper, ""Two"""');
    expect(lines[1]).toBe('problem,问题 A,');
  });

  it('is byte-stable across repeated calls', () => {
    expect(comparisonToCsv(TABLE)).toBe(comparisonToCsv(TABLE));
  });

  it('covers every backend field with a label', () => {
    // 与后端 COMPARISON_FIELDS 对齐的九个字段都要有中文标签，缺了界面会露英文字段名
    for (const field of [
      'problem',
      'method',
      'findings',
      'limitations',
      'purpose',
      'mechanism',
      'baseline',
      'dataset',
      'protocol',
    ]) {
      expect(COMPARISON_FIELD_META[field], field).toBeTruthy();
    }
  });
});
