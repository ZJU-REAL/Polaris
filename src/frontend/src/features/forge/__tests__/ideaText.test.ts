import { describe, expect, it } from 'vitest';
import { splitEnumeratedLine, splitInlineEnumerations } from '../ideaText';

/**
 * 想法的「方法概述 / 风险」等小节常是一整段「……：1) …；2) …；3) …」。
 * 明确是编号列举的拆成列表；其余文字必须原样不动。
 */
describe('idea text: inline enumerations become lists', () => {
  it('splits "1) … 2) … 3) …" into a lead sentence and an ordered list', () => {
    const line = '方法分三步：1) 构建跨域检索语料；2) 训练对比式检索器，引入难负例；3) 在下游任务上评估迁移效果。';
    expect(splitEnumeratedLine(line)).toEqual([
      '方法分三步：',
      '',
      '1. 构建跨域检索语料',
      '2. 训练对比式检索器，引入难负例',
      '3. 在下游任务上评估迁移效果。',
    ]);
  });

  it('handles full-width and parenthesised numbers, and circled digits', () => {
    expect(splitEnumeratedLine('主要风险：（1）标注成本偏高；（2）基线可能已经饱和。')).toEqual([
      '主要风险：',
      '',
      '1. 标注成本偏高',
      '2. 基线可能已经饱和。',
    ]);
    expect(splitEnumeratedLine('We test (1) retrieval recall on held-out domains, (2) answer faithfulness.')).toEqual([
      'We test',
      '',
      '1. retrieval recall on held-out domains',
      '2. answer faithfulness.',
    ]);
    expect(splitEnumeratedLine('①先收集失败案例 ②再归纳错误类型 ③最后设计修复策略')).toEqual([
      '1. 先收集失败案例',
      '2. 再归纳错误类型',
      '3. 最后设计修复策略',
    ]);
  });

  it('keeps the letters for "(a) … (b) …"', () => {
    expect(splitEnumeratedLine('消融实验包括 (a) 去掉重排序模块；(b) 换用稀疏检索。')).toEqual([
      '消融实验包括',
      '',
      '- (a) 去掉重排序模块',
      '- (b) 换用稀疏检索。',
    ]);
  });

  it('moves a sentence that follows the enumeration out of the last item', () => {
    const line = '分两步：1) 训练轻量判别器；2) 只在偏离时触发反思。判别器不需要额外人工标注。';
    expect(splitEnumeratedLine(line)).toEqual([
      '分两步：',
      '',
      '1. 训练轻量判别器',
      '2. 只在偏离时触发反思。',
      '',
      '判别器不需要额外人工标注。',
    ]);
    // 各项之间不是分号隔开的：分不清句号后面属于谁，保持在最后一项里
    expect(splitEnumeratedLine('①先收集失败案例 ②再归纳错误类型。然后设计修复策略。')).toEqual([
      '1. 先收集失败案例',
      '2. 再归纳错误类型。然后设计修复策略。',
    ]);
  });

  it('leaves normal prose alone', () => {
    const untouched = [
      '我们沿用式(1)的损失，并与式(2)的变体对比。',
      '在 2) 这一步之后模型收敛。',
      '结果见图(1)，其中 (2) 是对照组。',
      '预算约 3) 万元（含 GPU 租用）。',
      '方案 (1) 与 (2) 的差异不大。',
      'F1 提升 2.5 个点（p<0.05），召回率 (R@10) 提升 4 个点。',
      '共两阶段：2) 微调；3) 评估。',
      '步骤 1) 准备数据；3) 评估结果。',
      '调用 `f(1)` 再调用 `g(2)` 得到两组结果用于对比。',
      '当 $f(1) > g(2)$ 时停止，否则继续迭代直到收敛。',
    ];
    for (const line of untouched) expect(splitEnumeratedLine(line)).toBeNull();
  });

  it('does not touch headings, existing lists, tables, quotes or code', () => {
    const source = [
      '## 方法概述',
      '',
      '- 已经是列表：1) 第一点内容；2) 第二点内容。',
      '1. 有序列表项：(1) 子点内容一；(2) 子点内容二。',
      '> 引用里：1) 第一点内容；2) 第二点内容。',
      '| 列 | 1) 第一点内容；2) 第二点内容 |',
      '```',
      '步骤：1) 第一点内容；2) 第二点内容。',
      '```',
    ].join('\n');
    expect(splitInlineEnumerations(source)).toBe(source);
  });

  it('rewrites only the enumerated paragraph inside a full idea body', () => {
    const source = [
      '## 方法概述',
      '',
      '整体思路是先检索后生成。具体分三步：1) 构建领域语料；2) 训练检索器；3) 端到端评估效果。',
      '',
      '## 风险',
      '',
      '数据规模可能不足。',
    ].join('\n');
    expect(splitInlineEnumerations(source)).toBe(
      [
        '## 方法概述',
        '',
        '整体思路是先检索后生成。具体分三步：',
        '',
        '1. 构建领域语料',
        '2. 训练检索器',
        '3. 端到端评估效果。',
        '',
        '## 风险',
        '',
        '数据规模可能不足。',
      ].join('\n'),
    );
  });

  it('returns text without enumerations unchanged, byte for byte', () => {
    const source = '## 动机\n\n现有方法在跨域场景下召回率低。\n\n\n\n## 风险\n\n无。\n';
    expect(splitInlineEnumerations(source)).toBe(source);
    expect(splitInlineEnumerations('')).toBe('');
  });
});
