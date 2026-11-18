import { tr } from '../../../lib/i18n';

/* ============================================================
   任务和步骤给人看的名字。

   引擎写的任务标题（goal）和步骤标题是给日志用的：只有中文，带着内部名和实现细节
   （「Idea Forge：…」「相关性打分（LLM）」「语义去重（embedding + rerank）」）。
   界面按任务类型 / 步骤的动作名出自己的说法，中英各一份；认不出的才退回引擎的原文
   （去掉括号里的实现细节）。纯函数，文案都在函数里 tr()。
   ============================================================ */

/** 去掉标题里括号内的实现细节：「相关性打分（LLM）」→「相关性打分」。 */
export function stripImplementationNote(title: string): string {
  return title
    .replace(/\s*[（(][^（()）]*(LLM|embedding|rerank|SSH|Swiss|Elo|Semantic Scholar|脚本|自动质检|候选集)[^（()）]*[）)]/gi, '')
    .trim();
}

/** 「前缀：主题」里的主题；没有冒号就是整句。 */
function subjectOf(goal: string): string {
  const i = goal.search(/[：:]/);
  return (i >= 0 ? goal.slice(i + 1) : goal).trim();
}

export interface TaskLike {
  kind: string;
  goal: string;
}

/**
 * 任务标题：做什么 + 对象。
 * 「Idea Forge：智能体自我反思」→「生成想法」；「实验验证：X」→「实验：X」。
 * 课题名不重复写进标题——任务总是在某个课题的页面里看到的。
 */
export function taskTitle(task: TaskLike): string {
  const subject = subjectOf(task.goal);
  const withSubject = (zh: string, en: string) =>
    subject ? tr(`${zh}：${subject}`, `${en}: ${subject}`) : tr(zh, en);
  switch (task.kind) {
    case 'idea_forge':
      return tr('生成想法', 'Generate ideas');
    case 'idea_review':
      return task.goal.includes('补赛') ? tr('想法评审（补评）', 'Idea review (rematch)') : tr('想法评审', 'Idea review');
    case 'idea_proposal':
      return tr('写研究方案', 'Write a research proposal');
    case 'experiment':
      return withSubject('实验', 'Experiment');
    case 'paper_writing':
      return withSubject('起草稿件', 'Draft manuscript');
    case 'paper_review':
      return withSubject('稿件评审', 'Manuscript review');
    case 'presentation':
      return withSubject('制作汇报幻灯片', 'Make slides');
    case 'discovery':
      return withSubject('寻找假设', 'Look for hypotheses');
    case 'wiki_bootstrap':
    case 'wiki_ingest': {
      if (task.goal.includes('简报')) return withSubject('写文献简报', 'Write the digest');
      if (task.goal.startsWith('从锚点论文扩展')) return withSubject('扩展参考文献', 'Follow references');
      if (task.goal.startsWith('每日自动同步')) return withSubject('更新文献库', 'Update library');
      return withSubject('检索文献', 'Find papers');
    }
    case 'daily_feed_sync': {
      const date = /\d{4}-\d{2}-\d{2}/.exec(task.goal)?.[0];
      return date ? tr(`获取每日新论文（${date}）`, `Fetch daily papers (${date})`) : tr('获取每日新论文', 'Fetch daily papers');
    }
    default:
      return stripImplementationNote(task.goal) || task.kind;
  }
}

/** 步骤的动作名 → 名字。固定流程的步骤都在这张表里；实验的轮次带着序号，单独处理。 */
const STEP_LABELS: Record<string, { zh: string; en: string }> = {
  // 文献
  'wiki.search_candidates': { zh: '检索论文', en: 'Search for papers' },
  'wiki.snowball': { zh: '扩展参考文献', en: 'Follow references' },
  'wiki.score_relevance': { zh: '筛选相关论文', en: 'Screen for relevance' },
  'wiki.fetch_extract': { zh: '获取全文', en: 'Get full text' },
  'wiki.compile': { zh: '写解读', en: 'Write summaries' },
  'wiki.link_concepts': { zh: '整理概念', en: 'Link concepts' },
  'wiki.daily_digest': { zh: '写文献简报', en: 'Write the digest' },
  'wiki.trend_synthesize': { zh: '总结近期趋势', en: 'Summarise recent trends' },
  'wiki.update_watermark': { zh: '记录同步时间', en: 'Save sync time' },
  // 每日新论文
  'daily.fetch': { zh: '获取订阅分类的新论文', en: 'Fetch new papers in your categories' },
  'daily.upsert': { zh: '去掉重复的论文', en: 'Remove duplicates' },
  'daily.cleanup': { zh: '清理过期论文', en: 'Clear expired papers' },
  'daily.embed': { zh: '建立检索索引', en: 'Build the search index' },
  'daily.sync_libraries': { zh: '更新文献库', en: 'Update libraries' },
  // 生成想法
  'forge.read_context': { zh: '阅读文献库', en: 'Read the library' },
  'forge.collect_signals': { zh: '寻找线索', en: 'Look for leads' },
  'forge.gap_analysis': { zh: '归纳研究空白', en: 'Identify research gaps' },
  'forge.generate': { zh: '提出想法', en: 'Propose ideas' },
  'forge.score': { zh: '给想法打分', en: 'Score the ideas' },
  'forge.dedup': { zh: '去掉重复的想法', en: 'Remove duplicate ideas' },
  'forge.persist': { zh: '保存想法', en: 'Save the ideas' },
  // 研究方案
  'proposal.goal': { zh: '确定研究目标', en: 'Set the research goal' },
  'proposal.related_work': { zh: '梳理相关工作', en: 'Position against related work' },
  'proposal.design': { zh: '设计研究方案', en: 'Design the approach' },
  'proposal.experiments': { zh: '制定实验与评估计划', en: 'Plan experiments and evaluation' },
  'proposal.novelty_check': { zh: '核查新颖性', en: 'Check novelty' },
  'proposal.risks': { zh: '列出风险与备选方案', en: 'List risks and fallbacks' },
  'proposal.assemble': { zh: '汇总成研究方案', en: 'Assemble the proposal' },
  'proposal.review_revise': { zh: '评审并修订', en: 'Review and revise' },
  // 想法评审
  'review.pair': { zh: '安排对比', en: 'Pair up ideas' },
  'review.match': { zh: '对比辩论', en: 'Debate' },
  'review.summarize': { zh: '汇总结果', en: 'Sum up the results' },
  // 实验
  'experiment.plan': { zh: '制定实验计划', en: 'Plan the experiment' },
  'experiment.setup': { zh: '搭建环境并生成代码', en: 'Set up and write the code' },
  'experiment.smoke': { zh: '试运行', en: 'Trial run' },
  'experiment.run': { zh: '运行', en: 'Run' },
  'experiment.analyze': { zh: '分析结果', en: 'Analyse results' },
  'experiment.figures': { zh: '生成图表', en: 'Make figures' },
  'experiment.report': { zh: '写实验报告', en: 'Write the report' },
  // 写作
  'writing.section': { zh: '撰写章节', en: 'Write a section' },
  'writing.compile': { zh: '编译稿件', en: 'Compile the manuscript' },
  'writing.related_work': { zh: '撰写相关工作', en: 'Write related work' },
  // 稿件评审
  'review.citation_check': { zh: '核对引用', en: 'Check citations' },
  'review.fact_check': { zh: '核对事实与数字', en: 'Check facts and numbers' },
  'review.render': { zh: '渲染稿件', en: 'Render the manuscript' },
  'review.referees': { zh: '三位评审员评审', en: 'Review by three referees' },
  'review.meta_review': { zh: '汇总评审意见', en: 'Sum up the reviews' },
  'review.guardrail': { zh: '给出结论', en: 'Give the verdict' },
  // 汇报幻灯片
  'present.collect': { zh: '读取论文和配图', en: 'Read the paper and figures' },
  'present.outline': { zh: '设计大纲', en: 'Outline the talk' },
  'present.slides': { zh: '写幻灯片内容', en: 'Write the slides' },
  'present.build': { zh: '生成幻灯片文件', en: 'Build the deck' },
  // 寻找假设
  'hypothesis.seed': { zh: '提出第一批假设', en: 'Propose the first hypotheses' },
  'hypothesis.expand': { zh: '扩展并核对假设', en: 'Expand and check hypotheses' },
  'hypothesis.prune': { zh: '淘汰站不住的假设', en: 'Drop hypotheses that don’t hold' },
  'discovery.summarize': { zh: '汇总假设', en: 'Sum up the hypotheses' },
  'discovery.write_ideas': { zh: '把假设写成想法', en: 'Write hypotheses up as ideas' },
};

export interface StepLike {
  action: string;
  title: string;
}

/**
 * 步骤的名字。带序号的步骤（第 N 轮运行 / 第 N 场辩论 / 第 N 轮扩展假设）保留序号；
 * 写作的章节步骤保留章节名。
 */
export function stepLabel(step: StepLike): string {
  const round = /第\s*(\d+)\s*(轮|场)/.exec(step.title)?.[1];
  switch (step.action) {
    case 'experiment.run':
      if (round) return tr(`第 ${round} 轮运行`, `Round ${round}: run`);
      break;
    case 'experiment.analyze':
      if (round) return tr(`第 ${round} 轮分析`, `Round ${round}: analysis`);
      break;
    case 'review.match':
      if (round) return tr(`第 ${round} 场对比`, `Debate ${round}`);
      break;
    case 'hypothesis.expand':
      if (round) return tr(`第 ${round} 轮：扩展并核对假设`, `Round ${round}: expand and check hypotheses`);
      break;
    case 'writing.section': {
      const section = step.title.replace(/^撰写\s*/, '').trim();
      if (section) return tr(`撰写 ${section}`, `Write ${section}`);
      break;
    }
    case 'wiki.search_candidates':
      if (step.title.includes('每日论文池')) return tr('从每日新论文里挑选', 'Pick from the daily papers');
      break;
  }
  const known = STEP_LABELS[step.action];
  if (known) return tr(known.zh, known.en);
  return stripImplementationNote(step.title) || step.action;
}
