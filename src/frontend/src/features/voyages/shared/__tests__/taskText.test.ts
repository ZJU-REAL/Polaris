import { describe, expect, it } from 'vitest';
import { setLang } from '../../../../lib/i18n';
import { stepLabel, stepResult, stripImplementationNote, taskStatusLine, taskTitle, taskTone } from '../taskText';

describe('one-line step results', () => {
  it('turns the raw observation into a sentence', () => {
    expect(stepResult('forge.read_context', { papers: 20, concepts: 2 })).toBe('读了 20 篇论文、2 个概念');
    expect(stepResult('forge.generate', { requested: 8, generated: 8 })).toBe('提出 8 个想法');
    expect(stepResult('forge.dedup', { dropped: 0 })).toBe('没有重复的想法');
    expect(stepResult('review.summarize', { matches: 4, failed: 0 })).toBe('完成 4 场对比');
    expect(stepResult('hypothesis.expand', { children: ['a', 'b'] })).toBe('提出 2 个新假设');
    expect(stepResult('discovery.write_ideas', { ideas: 5, duplicates: 0 })).toBe('写成 5 个想法');
    expect(stepResult('daily.upsert', { created: 433, merged: 139 })).toBe('新增 433 篇，139 篇是重复的');
  });

  it('says nothing for a step it does not know, rather than dumping the data', () => {
    expect(stepResult('plugin.something', { anything: 1 })).toBeNull();
  });
});

describe('the status line at the top of a task', () => {
  const steps = [
    { action: 'wiki.search_candidates', title: '按查询词检索 arXiv', status: 'passed' },
    { action: 'wiki.score_relevance', title: '相关性打分（LLM）', status: 'running' },
    { action: 'wiki.compile', title: 'Librarian 编译 wiki 页', status: 'pending' },
  ];

  it('says what is happening now, in plain words', () => {
    expect(taskStatusLine({ status: 'executing' }, steps)).toBe('正在筛选相关论文（第 2 步，共 3 步）');
    expect(taskStatusLine({ status: 'planning' }, [])).toBe('正在安排步骤');
  });

  it('says what it needs from you', () => {
    expect(taskStatusLine({ status: 'paused_ask' }, steps)).toBe('等你回答一个问题，回答后继续');
    expect(taskStatusLine({ status: 'paused_gate' }, steps)).toBe('等你审批，批准后继续');
    const failed = [steps[0]!, { ...steps[1]!, status: 'failed' }, steps[2]!];
    expect(taskStatusLine({ status: 'paused_error' }, failed)).toBe('停在「筛选相关论文」这一步，需要你处理');
    // 排了自动重试：不需要你
    expect(taskStatusLine({ status: 'paused_error', retry_at: '2026-11-18T10:00:00Z' }, failed)).toBe(
      '「筛选相关论文」遇到问题，稍后自动重试',
    );
  });

  it('says how it ended', () => {
    const done = steps.map((s) => ({ ...s, status: 'passed' }));
    expect(taskStatusLine({ status: 'done' }, done)).toBe('已完成，共 3 步');
    expect(taskStatusLine({ status: 'cancelled' }, steps)).toBe('已取消');
    expect(taskTone('done')).toBe('ok');
    expect(taskTone('paused_ask')).toBe('warn');
    expect(taskTone('failed')).toBe('err');
    expect(taskTone('executing')).toBe('active');
  });
});

/* 任务和步骤给人看的名字：不带内部名、不带实现细节，中英各一份。 */

/** 引擎实际写出来的步骤（动作名 + 标题），见 backend/app/agents/voyage/navigator.py。 */
const ENGINE_STEPS: [string, string][] = [
  ['wiki.search_candidates', '按查询词检索 arXiv'],
  ['wiki.snowball', '从锚点论文扩展（Semantic Scholar）'],
  ['wiki.score_relevance', '相关性打分（LLM）'],
  ['wiki.fetch_extract', '下载 PDF + 抽全文'],
  ['wiki.compile', 'Librarian 编译 wiki 页'],
  ['wiki.link_concepts', '概念上链 + embedding'],
  ['wiki.daily_digest', '生成每日简报'],
  ['wiki.trend_synthesize', '综合滚动趋势'],
  ['wiki.update_watermark', '记录同步进度'],
  ['forge.read_context', '读取知识库上下文'],
  ['forge.collect_signals', '信号采集（组合空白/趋势/局限）'],
  ['forge.gap_analysis', '研究空白综合'],
  ['forge.generate', '生成候选 idea（LLM）'],
  ['forge.score', '四维打分（LLM 逐条）'],
  ['forge.dedup', '语义去重（embedding + rerank）'],
  ['forge.persist', '入库候选池'],
  ['review.pair', '配对（Swiss：按 Elo 相邻配对）'],
  ['review.summarize', '锦标赛汇总'],
  ['experiment.plan', '实验计划（LLM）'],
  ['experiment.setup', '建环境（SSH + 代码生成）'],
  ['experiment.smoke', '冒烟测试'],
  ['experiment.figures', '实验图表（脚本生成 + 自动质检）'],
  ['experiment.report', '实验报告（LLM）'],
  ['writing.related_work', '撰写 Related Work（候选集内选引）'],
  ['review.referees', '评审员评审（×3）'],
  ['review.guardrail', 'guardrail 校验'],
  ['hypothesis.seed', '生成根假设'],
  ['discovery.write_ideas', '把假设写成想法'],
];

const JARGON = /LLM|embedding|rerank|wiki|Librarian|Swiss|\bElo\b|guardrail|SSH|上链|入库|冒烟|水位|锚点/i;

describe('step labels', () => {
  it('names every engine step in plain words, in both languages', () => {
    for (const lang of ['zh', 'en'] as const) {
      setLang(lang);
      for (const [action, title] of ENGINE_STEPS) {
        const label = stepLabel({ action, title });
        expect(label, `${lang} ${action}`).not.toMatch(JARGON);
        expect(label.length, `${lang} ${action}`).toBeGreaterThan(1);
        if (lang === 'en') expect(label, action).not.toMatch(/[一-鿿]/);
      }
    }
    setLang('zh');
  });

  it('keeps the round number of repeated steps', () => {
    expect(stepLabel({ action: 'experiment.run', title: '第 2 轮运行' })).toBe('第 2 轮运行');
    expect(stepLabel({ action: 'experiment.analyze', title: '第 3 轮分析' })).toBe('第 3 轮分析');
    expect(stepLabel({ action: 'review.match', title: '第 4 场辩论' })).toBe('第 4 场对比');
    expect(stepLabel({ action: 'hypothesis.expand', title: '第 2 轮扩展假设' })).toBe('第 2 轮：扩展并核对假设');
    expect(stepLabel({ action: 'writing.section', title: '撰写 Introduction' })).toBe('撰写 Introduction');
  });

  it('falls back to the engine title without the bracketed implementation note', () => {
    expect(stepLabel({ action: 'plugin.something', title: '整理数据（LLM）' })).toBe('整理数据');
    expect(stripImplementationNote('语义去重（embedding + rerank）')).toBe('语义去重');
    // 括号里是内容而不是实现细节的，留着
    expect(stripImplementationNote('获取每日新论文（2026-11-18）')).toBe('获取每日新论文（2026-11-18）');
  });
});

describe('task titles', () => {
  it('says what the task does, without internal names', () => {
    expect(taskTitle({ kind: 'idea_forge', goal: 'Idea Forge：智能体自我反思' })).toBe('生成想法');
    expect(taskTitle({ kind: 'idea_review', goal: 'Idea 评审锦标赛：智能体自我反思' })).toBe('想法评审');
    expect(taskTitle({ kind: 'idea_proposal', goal: '深度研究方案：智能体自我反思' })).toBe('写研究方案');
    expect(taskTitle({ kind: 'experiment', goal: '实验验证：过程级反思' })).toBe('实验：过程级反思');
    expect(taskTitle({ kind: 'paper_writing', goal: '论文撰写：ReflectStep' })).toBe('起草稿件：ReflectStep');
    expect(taskTitle({ kind: 'wiki_bootstrap', goal: '按查询词检索入库：RAG 联合训练' })).toBe('检索文献：RAG 联合训练');
    expect(taskTitle({ kind: 'wiki_ingest', goal: '每日自动同步：RAG' })).toBe('更新文献库：RAG');
    expect(taskTitle({ kind: 'daily_feed_sync', goal: '每日新论文抓取（2026-11-18）' })).toBe('获取每日新论文（2026-11-18）');
    expect(taskTitle({ kind: 'discovery', goal: '检索器与生成器的联合训练' })).toBe('寻找假设：检索器与生成器的联合训练');
  });

  it('is English in English', () => {
    setLang('en');
    expect(taskTitle({ kind: 'idea_forge', goal: 'Idea Forge：X' })).toBe('Generate ideas');
    expect(taskTitle({ kind: 'experiment', goal: '实验验证：Process-level reflection' })).toBe('Experiment: Process-level reflection');
    setLang('zh');
  });
});
