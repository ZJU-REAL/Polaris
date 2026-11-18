import { describe, expect, it } from 'vitest';
import {
  boldLeadLabels,
  cleanSnippet,
  humanizeFieldNames,
  splitBodySections,
  splitEnumeratedLine,
  splitInlineEnumerations,
  tidyProposalSections,
} from '../ideaText';
import { plainSummary } from '../../../lib/format';

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

/* 下面这些都来自真实模型跑出来的想法和论文片段。 */
describe('idea text: what real runs look like', () => {
  it('splits upper-case lettered enumerations too', () => {
    const line = '对照系统：(A) 完全冻结文档侧的基线；(B) 端到端联合训练并周期性重建索引；(C) 本文提出的离线流程。';
    expect(splitEnumeratedLine(line)).toEqual(['对照系统：', '', '- (A) 完全冻结文档侧的基线', '- (B) 端到端联合训练并周期性重建索引', '- (C) 本文提出的离线流程。']);
    // 句子里提到 A/B/C 不是列举
    expect(splitEnumeratedLine('在每个域上跑 A/B/C 五个系统，C 相对 A 的增益能否覆盖 B 相对 A 增益的 85%。')).toBeNull();
  });

  it('bolds the label that leads a long paragraph, and nothing else', () => {
    const long = '用适配后的文档编码器对全库重编码一次，之后索引完全冻结，不再刷新，随后只微调查询侧与生成器。';
    expect(boldLeadLabels(`阶段二（一次性索引重建）。${long}`)).toBe(`**阶段二（一次性索引重建）。**${long}`);
    expect(boldLeadLabels(`实验 1（主对比）：${long}`)).toBe(`**实验 1（主对比）：**${long}`);
    expect(boldLeadLabels(`缓解：${long}`)).toBe(`**缓解：**${long}`);
    // 括号说明里可以有逗号
    expect(boldLeadLabels(`实验一（偶极标定，针对未证实命题1）：${long}`)).toBe(`**实验一（偶极标定，针对未证实命题1）：**${long}`);
    // 十几个字带冒号的是一句话，不是标签；短段落、列表、标题不动
    const sentence = `这两条线索共同提示一个尚未被检验的可能：${long}`;
    expect(boldLeadLabels(sentence)).toBe(sentence);
    expect(boldLeadLabels('核心假设：文档侧更新是领域适配。')).toBe('核心假设：文档侧更新是领域适配。');
    expect(boldLeadLabels(`- 缓解：${long}`)).toBe(`- 缓解：${long}`);
    expect(boldLeadLabels(`## 风险：${long}`)).toBe(`## 风险：${long}`);
    // 只看段首：段落中间的行不动
    expect(boldLeadLabels(`第一行。\n缓解：${long}`)).toBe(`第一行。\n缓解：${long}`);
  });

  it('cuts the body into sections by its top-level headings', () => {
    const md = ['开头一段。', '', '## 动机', '', '甲。', '', '### 小标题', '', '乙。', '', '## 风险', '', '丙。'].join('\n');
    const secs = splitBodySections(md);
    expect(secs.map((s) => s.title)).toEqual(['', '动机', '风险']);
    expect(secs[1]!.body).toContain('### 小标题');
    expect(secs[2]!.chars).toBe(2);
    expect(new Set(secs.map((s) => s.id)).size).toBe(3);
    expect(splitBodySections('只有一段，没有标题。')).toHaveLength(1);
    expect(splitBodySections('   ')).toEqual([]);
    // 代码块里的 # 不是标题
    expect(splitBodySections(['## 方法', '```', '## not a heading', '```'].join('\n'))).toHaveLength(1);
  });

  it('turns a raw PDF passage into readable text with honest ellipses', () => {
    const raw =
      'inations of retrievers and\nLMs. For code retrieval tasks, larger LMs can yield greater gains in retriever performance.\n•\nRevela learns efficiently, demonstrat-\ning strong generalization. As we will see, the impact of\nﬁxing the documents encoder varies greatly for diﬀerent tasks when a large training dataset is avail';
    const s = cleanSnippet(raw);
    expect(s.cutStart).toBe(true);
    expect(s.cutEnd).toBe(true);
    expect(s.text.startsWith('…of retrievers and LMs.')).toBe(true);
    expect(s.text).toContain('demonstrating strong generalization');
    expect(s.text).toContain('fixing the documents encoder varies greatly for different tasks');
    expect(s.text).toContain('• Revela learns');
    expect(s.text.endsWith('dataset is…')).toBe(true);
    expect(s.text).not.toContain('\n');
    // 完整的句子不加省略号；真连字符保留
    expect(cleanSnippet('Query-side fine-tuning decouples the encoders.')).toEqual({ text: 'Query-side fine-tuning decouples the encoders.', cutStart: false, cutEnd: false });
    expect(cleanSnippet('  ').text).toBe('');
    // 片段以论文标题起头：标题不重复
    expect(cleanSnippet('Buried Interface Engineering: a Key to SAM\nRuida Xu, Chengji Wang. In recent years, PSCs improved.', 'Buried Interface Engineering: a Key to SAM').text).toBe('Ruida Xu, Chengji Wang. In recent years, PSCs improved.');
  });

  it('turns hand-numbered paragraphs into a real list', () => {
    expect(splitInlineEnumerations('1) 偶极轴的构建与标定。\n\n2）钝化能力的解耦。')).toBe('1. 偶极轴的构建与标定。\n\n2. 钝化能力的解耦。');
    expect(splitInlineEnumerations('见式 1) 的推导')).toBe('见式 1) 的推导');
  });

  it('replaces leaked field names in score reasons', () => {
    expect(humanizeFieldNames('novel_ratio 为 0.5，support_ratio 仅 0.5；risk_note 另指出')).toBe('新颖论断比例 为 0.5，文献支持比例 仅 0.5；风险说明 另指出');
    expect(humanizeFieldNames('support_ratio is 0.5')).toBe('share of supported claims is 0.5');
    expect(humanizeFieldNames('没有字段名的句子')).toBe('没有字段名的句子');
  });

  it('strips citation markers and wiki links from a one-line summary', () => {
    expect(plainSummary('重新表述为无监督学习问题。[EVIDENCE p=1·s=5] 在五个数据集上超过基线')).toBe('重新表述为无监督学习问题。在五个数据集上超过基线');
    expect(plainSummary('端到端反向传播训练出来的 [文1·句3][文1·句4]。在三个榜单上')).toBe('端到端反向传播训练出来的。在三个榜单上');
    expect(plainSummary('把 [[检索增强语言模型]] 用于 **少样本** 学习')).toBe('把 检索增强语言模型 用于 少样本 学习');
    expect(plainSummary('BLEU [1] 提升，区间 [0, 1]')).toBe('BLEU [1] 提升，区间 [0, 1]');
    expect(plainSummary('端到端训练出来的 [文1·句3][文1·')).toBe('端到端训练出来的');
    expect(plainSummary(null)).toBe('');
  });
});

describe('a real proposal body repeats what the page already shows', () => {
  const md = [
    '# 桥接还是联合训练？冻结检索器 + 轻量桥接模块的等价性受控检验',
    '',
    '## 研究目标',
    '',
    '- **研究类型**：analysis',
    '',
    '## 背景与相关工作',
    '',
    '# 背景与相关工作',
    '',
    '## 1. 问题背景',
    '',
    '检索器要不要训。',
    '',
    '# 研究方案设计',
    '',
    '## 1. 核心假设',
    '',
    '本研究把联合训练拆成两个命题。',
  ].join('\n');

  it('drops the title section, the repeated goal and dangling headings', () => {
    const secs = tidyProposalSections(splitBodySections(md), '桥接还是联合训练？冻结检索器 + 轻量桥接模块的等价性受控检验', true);
    expect(secs.map((s) => s.title)).toEqual(['背景与相关工作', '研究方案设计']);
    expect(secs[0]!.body).toContain('检索器要不要训');
    expect(secs.map((s) => s.body).join('\n')).not.toContain('研究类型');
  });

  it('keeps the goal when the page has no goal table', () => {
    const secs = tidyProposalSections(splitBodySections(md), '另一个标题', false);
    expect(secs[0]!.body).toContain('研究类型');
    // 末尾挂着的空标题仍然去掉
    expect(secs[0]!.body.trim().endsWith('## 背景与相关工作')).toBe(false);
  });
});
