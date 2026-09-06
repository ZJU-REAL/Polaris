/* ============================================================
   论文对比表（#669）：类型 + 纯函数（CSV 拼装）。
   与后端 services/comparison.py 对齐：行 = skeleton/method 的九个
   抽取字段，列 = 所选论文；cell 带 present/schema_id/extracted_at
   溯源。纯函数抽在这里（不碰 DOM / fetch），vitest 直接测。
   ============================================================ */

export interface ComparisonCell {
  /** 渲染串（list 字段后端已用中文分号拼好）；没抽到为 null */
  value: string | null;
  /** false = 没抽过该 schema，或抽过但该字段为空 → 界面显示「未抽取」而非空白 */
  present: boolean;
  schema_id: string;
  extracted_at: string | null;
}

export interface ComparisonRow {
  field: string;
  schema_id: string;
  /** 与 papers 同序同长 */
  cells: ComparisonCell[];
}

export interface ComparisonPaper {
  paper_id: string;
  title: string;
  year: number | null;
}

export interface ComparisonTable {
  papers: ComparisonPaper[];
  rows: ComparisonRow[];
}

/* 字段中文/英文标签：模块级常量按约定保留 {zh, en}，渲染处再 tr()
   （i18n 约定：顶层 tr 不随语言切换重新求值）。 */
export const COMPARISON_FIELD_META: Record<string, { zh: string; en: string }> = {
  problem: { zh: '研究问题', en: 'Problem' },
  method: { zh: '方法概述', en: 'Method' },
  findings: { zh: '主要发现', en: 'Findings' },
  limitations: { zh: '局限', en: 'Limitations' },
  purpose: { zh: '方法目的', en: 'Purpose' },
  mechanism: { zh: '核心机制', en: 'Mechanism' },
  baseline: { zh: '基线', en: 'Baselines' },
  dataset: { zh: '数据集', en: 'Datasets' },
  protocol: { zh: '实验流程', en: 'Protocol' },
};

/** RFC 4180 口径的单元格转义：含逗号/引号/换行才加引号，内部引号翻倍。 */
export function csvEscape(cell: string): string {
  if (/[",\r\n]/.test(cell)) return `"${cell.replace(/"/g, '""')}"`;
  return cell;
}

/** 列头：标题 + 年份（有才加）。年份进表头是为了导出后离线看也分得清同名论文。 */
export function comparisonColumnTitle(paper: ComparisonPaper): string {
  return paper.year != null ? `${paper.title} (${paper.year})` : paper.title;
}

/**
 * 把对比表拼成 CSV 文本（不含 BOM——BOM 属下载环节的编码关注点，纯函数不掺）。
 *
 * 行序 = table.rows 原序，列序 = table.papers 原序（即用户勾选顺序），
 * 两端各自稳定，重复导出逐字节一致。absent cell 落 absentText
 * （界面上的「未抽取」由调用方传进来，纯函数不内置文案）。
 */
export function comparisonToCsv(
  table: ComparisonTable,
  opts: {
    fieldLabel?: (field: string) => string;
    absentText?: string;
    /** 左上角表头（默认 'field'；界面导出时传「字段」的当前语言文案） */
    fieldColumnTitle?: string;
  } = {},
): string {
  const label = opts.fieldLabel ?? ((f: string) => f);
  const absent = opts.absentText ?? '';
  const lines: string[] = [];
  lines.push(
    [opts.fieldColumnTitle ?? 'field', ...table.papers.map(comparisonColumnTitle)]
      .map(csvEscape)
      .join(','),
  );
  for (const row of table.rows) {
    lines.push(
      [
        label(row.field),
        ...row.cells.map((cell) => (cell.present && cell.value != null ? cell.value : absent)),
      ]
        .map(csvEscape)
        .join(','),
    );
  }
  // 末尾带换行：POSIX 文本文件口径，也让追加式 diff 干净
  return lines.join('\r\n') + '\r\n';
}
