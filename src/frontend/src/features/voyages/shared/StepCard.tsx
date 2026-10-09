import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Icon } from '../../../components/ui/Icon';
import { topicPath, useProject } from '../../../app/project';
import { fmtDuration, fmtTime, fmtTokens } from '../../../lib/format';
import { tr } from '../../../lib/i18n';
import type {
  VoyageAcceptance,
  VoyageAcceptanceCheck,
  VoyagePlanEvent,
  VoyageStepAttempt,
  VoyageStepRead,
} from '../../../lib/api';
import { asObj, enCount, num, obsoleteReasonOf, PLAN_SOURCE, stepTokenCount } from './stepUtils';
import { StatusDot, TaskStatus } from './StatusDot';

/* ============================================================
   步骤卡（StepCard）与其子块：验收标准 / 判定 / 尝试记录 / 原始观测 /
   计划调整分隔卡 / wiki 与 experiment 的用户可读摘要。
   从 VoyageDetailPage 抽出，供任务详情页与实验运行台共用。
   ============================================================ */

// —— 文献任务（wiki_bootstrap / wiki_ingest）：observation 的用户可读摘要 ——

export interface PaperBrief {
  id: string;
  title: string;
  score?: number;
  passed?: boolean;
  pdf?: boolean;
}

function briefsOf(v: unknown): PaperBrief[] {
  if (!Array.isArray(v)) return [];
  return v.filter(
    (x): x is PaperBrief => !!x && typeof x === 'object' && typeof (x as PaperBrief).title === 'string',
  );
}

function namesOf(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
}

/** 源 id 的显示名。认不出的原样显示——插件装的新源也看得懂是哪个。 */
const SOURCE_LABELS: Record<string, string> = {
  arxiv: 'arXiv',
  openalex: 'OpenAlex',
  semantic: 'Semantic Scholar',
  pubmed: 'PubMed',
  europepmc: 'Europe PMC',
  crossref: 'Crossref',
  hal: 'HAL',
  core: 'CORE',
  base: 'BASE',
  sciverse: 'ScienceDirect',
};

export function sourceLabel(id: string): string {
  return SOURCE_LABELS[id] ?? id;
}

export interface WikiStepFriendly {
  /** 一句给用户看的中文小结 */
  text: string;
  /** 涉及的论文清单（可点击跳文献库）；后端最多回传 30 篇节选 */
  papers: PaperBrief[];
  /** 该步骤实际涉及的论文总数（清单可能只是节选） */
  papersTotal?: number;
  /** 新概念名（wiki.link_concepts） */
  concepts?: string[];
}

/** 把 wiki.* 动作的 observation 翻译成用户可读摘要；不认识的动作返回 null（回退原始 JSON）。 */
export function wikiStepFriendly(action: string, obs: Record<string, unknown>): WikiStepFriendly | null {
  const failedCount = Array.isArray(obs.failed) ? obs.failed.length : 0;
  switch (action) {
    case 'wiki.search_candidates': {
      // 候选来源随模式而变：建库按库的来源检索；增量从每日池里取库的来源带来的那部分，
      // 没有日更的源（OpenAlex、Crossref……）另按关键词检索（#821）。按 mode 分支——
      // 以前按 source === 'daily_feed' 判断，而且检索那一支一律写「从 arXiv 检索到」，
      // 选了 PubMed 的库也这么显示。
      if (obs.mode === 'incremental') {
        const searched = namesOf(obs.searched_sources);
        const parts: string[] = [];
        if (!searched.length || num(obs.feed_total) > 0 || String(obs.source ?? '').startsWith('daily_feed')) {
          parts.push(
            tr(
              `每日新论文 ${num(obs.feed_total)} 篇，初筛 ${num(obs.after_vector_rank)} 篇，已在库 ${num(obs.already_in_library)} 篇`,
              `${num(obs.feed_total)} in the daily feed, ${num(obs.after_vector_rank)} shortlisted, ${num(obs.already_in_library)} already in the library`,
            ),
          );
        }
        if (searched.length) {
          parts.push(
            tr(
              `${searched.map(sourceLabel).join('、')} 检索到 ${num(obs.search_fetched)} 篇`,
              `${num(obs.search_fetched)} from ${searched.map(sourceLabel).join(', ')}`,
            ),
          );
        }
        return {
          text:
            tr(`新收录 ${num(obs.inserted)} 篇`, enCount(num(obs.inserted), 'new paper')) +
            tr('（', ' (') +
            parts.join(tr('；', '; ')) +
            tr('）', ')'),
          papers: briefsOf(obs.new_papers),
          papersTotal: num(obs.inserted),
        };
      }
      const used = String(obs.source ?? '')
        .split(',')
        .map((x) => x.trim())
        .filter((x) => x && x !== 'none');
      const where = used.length ? used.map(sourceLabel).join(tr('、', ', ')) : tr('数据源', 'the sources');
      return {
        text: tr(
          `新收录 ${num(obs.inserted)} 篇（在 ${where} 找到 ${num(obs.found)} 篇）`,
          `${enCount(num(obs.inserted), 'new paper')} (${num(obs.found)} found on ${where})`,
        ),
        papers: briefsOf(obs.new_papers),
        papersTotal: num(obs.inserted),
      };
    }
    case 'wiki.snowball':
      if (obs.skipped) return { text: tr('已跳过：未开启参考文献扩展', 'Skipped: reference expansion is off'), papers: [] };
      return {
        text:
          tr(
            `从 ${num(obs.processed)} 篇论文的参考文献和引用中新收录 ${num(obs.inserted)} 篇`,
            `${enCount(num(obs.inserted), 'new paper')} from the references and citations of ${num(obs.processed)}`,
          ) +
          (failedCount ? tr(`，${failedCount} 篇查询失败`, `; ${failedCount} lookups failed`) : ''),
        papers: briefsOf(obs.new_papers),
        papersTotal: num(obs.inserted),
      };
    case 'wiki.score_relevance': {
      const passed = num(obs.succeeded) - num(obs.excluded);
      return {
        text:
          tr(
            `${passed} 篇相关，${num(obs.excluded)} 篇不相关已移除（共评估 ${num(obs.processed)} 篇）`,
            `${passed} relevant, ${num(obs.excluded)} removed as off-topic (${num(obs.processed)} checked)`,
          ) +
          (failedCount ? tr(`，${failedCount} 篇评估失败`, `; ${failedCount} couldn’t be checked`) : ''),
        papers: briefsOf(obs.scored_papers),
        papersTotal: num(obs.succeeded),
      };
    }
    case 'wiki.fetch_extract':
      return {
        text:
          tr(`获取 ${num(obs.processed)} 篇论文的全文`, `Fetched full text for ${enCount(num(obs.processed), 'paper')}`) +
          (num(obs.degraded)
            ? tr(`，其中 ${num(obs.degraded)} 篇无全文，改用摘要`, `; ${num(obs.degraded)} had none, so the abstract is used`)
            : ''),
        papers: briefsOf(obs.fetched_papers),
        papersTotal: num(obs.processed),
      };
    case 'wiki.compile':
      return {
        text:
          tr(`生成 ${num(obs.succeeded)} 篇解读`, `Generated ${enCount(num(obs.succeeded), 'summary', 'summaries')}`) +
          (failedCount ? tr(`，${failedCount} 篇失败，下次同步时重试`, `; ${failedCount} failed and will retry on the next sync`) : ''),
        papers: briefsOf(obs.compiled_papers),
        papersTotal: num(obs.succeeded),
      };
    case 'wiki.link_concepts':
      return {
        text: tr(
          `新增 ${num(obs.concepts_promoted)} 个概念，关联论文 ${num(obs.links_created)} 次`,
          `${enCount(num(obs.concepts_promoted), 'new concept')}, ${enCount(num(obs.links_created), 'paper link')}`,
        ),
        papers: [],
        concepts: namesOf(obs.new_concepts),
      };
    case 'wiki.update_watermark':
      return {
        text: obs.watermark
          ? tr(
              `下次从 ${String(obs.watermark).slice(0, 10)} 起同步`,
              `Next sync starts from ${String(obs.watermark).slice(0, 10)}`,
            )
          : tr('已记录同步时间', 'Sync time saved'),
        papers: [],
      };
    default:
      return null;
  }
}

const PAPER_LIST_PREVIEW = 5;

function StepPaperList({ papers, total }: { papers: PaperBrief[]; total?: number }) {
  const navigate = useNavigate();
  const { currentProjectId } = useProject();
  const [open, setOpen] = useState(false);
  if (papers.length === 0) return null;
  const shown = open ? papers : papers.slice(0, PAPER_LIST_PREVIEW);
  // 后端 observation 里的清单最多 30 篇节选，total 才是该步骤实际论文数
  const truncated = typeof total === 'number' && total > papers.length;
  return (
    <div className="col" style={{ gap: 3, marginTop: 8 }}>
      {shown.map((p) => (
        <div key={p.id} className="row gap6" style={{ fontSize: 12, minWidth: 0 }}>
          {p.passed === false ? (
            <Icon name="x" size={11} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
          ) : (
            <Icon name="book" size={11} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
          )}
          <span
            style={{
              cursor: 'pointer',
              color: p.passed === false ? 'var(--text-3)' : 'var(--text-2)',
              textDecoration: p.passed === false ? 'line-through' : 'none',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              minWidth: 0,
            }}
            title={p.title}
            onClick={() => navigate(topicPath(currentProjectId, `wiki?paper=${p.id}`))}
          >
            {p.title}
          </span>
          {typeof p.score === 'number' && (
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', flexShrink: 0 }}>
              {p.score.toFixed(2)}
            </span>
          )}
          {p.pdf === false && (
            <span className="mono" style={{ fontSize: 10, color: 'var(--warn-tx)', flexShrink: 0 }}>{tr('无 PDF', 'no PDF')}</span>
          )}
        </div>
      ))}
      {papers.length > PAPER_LIST_PREVIEW && (
        <button
          onClick={() => setOpen(!open)}
          style={{ border: 'none', background: 'transparent', cursor: 'pointer', padding: 0, fontSize: 12, color: 'var(--accent-text)', textAlign: 'left' }}
        >
          {open
            ? tr('收起', 'Collapse')
            : truncated
              ? tr(`显示前 ${papers.length} 篇（共 ${total} 篇）`, `Show first ${papers.length} of ${total}`)
              : tr(`显示全部 ${papers.length} 篇`, `Show all ${papers.length}`)}
        </button>
      )}
    </div>
  );
}

export function WikiStepSummary({ friendly }: { friendly: WikiStepFriendly }) {
  return (
    <div
      style={{
        marginTop: 10,
        padding: '9px 12px',
        background: 'var(--surface-2)',
        borderRadius: 9,
        fontSize: 13,
        lineHeight: 1.6,
        color: 'var(--text)',
      }}
    >
      {friendly.text}
      <StepPaperList papers={friendly.papers} total={friendly.papersTotal} />
      {friendly.concepts && friendly.concepts.length > 0 && (
        <div className="row gap6 wrap" style={{ marginTop: 8 }}>
          {friendly.concepts.map((name) => (
            <span key={name} className="tag" style={{ fontSize: 11 }}>
              {name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// —— 验收标准 / 判定 ——

/** 结构化检查项 → 大白话（未知 kind 原样展示）。 */
function checkText(c: VoyageAcceptanceCheck): string {
  switch (c.kind) {
    case 'no_error':
      return tr('执行无报错', 'Runs without errors');
    case 'exit_code':
      return tr(`退出码为 ${String(c.value ?? 0)}`, `Exit code is ${String(c.value ?? 0)}`);
    case 'artifact_exists':
      return tr(`产物 ${String(c.key ?? '')} 已生成`, `Artifact ${String(c.key ?? '')} exists`);
    case 'schema_valid': {
      const keys = Array.isArray(c.required_keys) ? c.required_keys.join(', ') : '';
      return tr(
        `${String(c.field ?? '')} 结构完整${keys ? `（需包含 ${keys}）` : ''}`,
        `${String(c.field ?? '')} has a valid structure${keys ? ` (must include ${keys})` : ''}`,
      );
    }
    case 'metric':
      return tr(
        `指标 ${String(c.name ?? '')} ${String(c.op ?? '')} ${String(c.value ?? '')}`,
        `Metric ${String(c.name ?? '')} ${String(c.op ?? '')} ${String(c.value ?? '')}`,
      );
    case 'min_count':
      return tr(
        `${String(c.field ?? '')} 数量 ≥ ${String(c.value ?? '')}`,
        `${String(c.field ?? '')} count ≥ ${String(c.value ?? '')}`,
      );
    case 'llm_rubric':
      return tr(`AI 评审：${String(c.rubric ?? '')}`, `AI review: ${String(c.rubric ?? '')}`);
    default:
      return c.kind;
  }
}

/** 验收标准区：这一步"怎样算通过"，默认收起。 */
export function AcceptanceBlock({ acceptance }: { acceptance: VoyageAcceptance }) {
  const [open, setOpen] = useState(false);
  const checks = acceptance.checks ?? [];
  if (checks.length === 0 && !acceptance.text) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <button
        className="row gap6"
        onClick={() => setOpen(!open)}
        style={{ border: 'none', background: 'transparent', cursor: 'pointer', padding: 0, fontSize: 12, fontWeight: 600, color: 'var(--text-3)' }}
      >
        <Icon name="chevDown" size={12} style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />
        {checks.length > 0
          ? tr(`验收标准（${checks.length} 项）`, `Pass criteria (${checks.length})`)
          : tr('验收标准', 'Pass criteria')}
      </button>
      {open && (
        <div style={{ marginTop: 6, padding: '8px 12px', background: 'var(--surface-2)', borderRadius: 8, fontSize: 12, lineHeight: 1.7, color: 'var(--text-2)' }}>
          {checks.map((c, i) => (
            <div key={i} className="row gap6" style={{ alignItems: 'flex-start' }}>
              <Icon name="check" size={11} style={{ color: 'var(--text-4)', flexShrink: 0, marginTop: 4 }} />
              <span style={{ minWidth: 0 }}>{checkText(c)}</span>
            </div>
          ))}
          {acceptance.text && (
            <div style={{ marginTop: checks.length > 0 ? 6 : 0, color: 'var(--text-3)', fontSize: 12 }}>
              {acceptance.text}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** 时间线里的「计划调整」分隔条目：解释为什么多出/作废了步骤。 */
export function PlanEventCard({ event }: { event: VoyagePlanEvent }) {
  const src = PLAN_SOURCE[event.source];
  return (
    <div
      style={{
        padding: '10px 14px',
        background: 'var(--accent-soft)',
        borderRadius: 10,
        fontSize: 13,
        lineHeight: 1.6,
      }}
    >
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        <span style={{ fontWeight: 600, color: 'var(--accent-text)' }}>
          <Icon name="refresh" size={12} style={{ display: 'inline-block', verticalAlign: '-1.5px', marginRight: 5 }} />
          {tr(`计划调整 #${event.iteration}`, `Plan adjustment #${event.iteration}`)}
        </span>
        <span className="pill sm" style={{ background: 'var(--surface)', color: 'var(--text-2)' }}>
          {src ? tr(src.zh, src.en) : event.source}
        </span>
        {event.at && <span className="mono muted" style={{ fontSize: 11, marginLeft: 'auto' }}>{fmtTime(event.at)}</span>}
      </div>
      {event.reason && <div style={{ marginTop: 4, color: 'var(--text)' }}>{event.reason}</div>}
      <div className="row gap10" style={{ marginTop: 4, flexWrap: 'wrap', fontSize: 11, color: 'var(--text-3)' }}>
        {event.added > 0 && <span>{tr(`新增 ${event.added} 步`, `${enCount(event.added, 'step')} added`)}</span>}
        {event.obsoleted > 0 && <span>{tr(`作废 ${event.obsoleted} 步`, `${enCount(event.obsoleted, 'step')} dropped`)}</span>}
        {event.trigger_step && (
          <span>{tr(`由「${event.trigger_step}」触发`, `After “${event.trigger_step}”`)}</span>
        )}
      </div>
    </div>
  );
}

// —— analyze 步骤的因果摘要（observation.plan_signal）——

/** stopped_reason → 大白话（未收录原样展示）。 */
export const STOPPED_REASON: Record<string, { zh: string; en: string }> = {
  no_improve: { zh: '连续几轮无提升', en: 'no further improvement' },
  max_runs: { zh: '达到轮次上限', en: 'round limit reached' },
  max_hours: { zh: '达到时长上限', en: 'time limit reached' },
  debug_limit: { zh: '修复次数用完', en: 'fix attempts used up' },
  hypotheses_resolved: { zh: '假设都已有结论', en: 'all hypotheses answered' },
};

/** plan_signal → 一句因果摘要（为什么后面多了/没多新步骤）。 */
export function planSignalText(sig: Record<string, unknown>): string | null {
  if (sig.decision === 'continue') {
    const nr = num(sig.next_round);
    return nr > 0
      ? tr(`继续迭代，已加入第 ${nr} 轮`, `Continuing: round ${nr} added`)
      : tr('继续迭代，已加入下一轮', 'Continuing: next round added');
  }
  if (sig.decision === 'finish') {
    const raw = typeof sig.stopped_reason === 'string' ? sig.stopped_reason : '';
    const m = STOPPED_REASON[raw];
    const reasonZh = m ? m.zh : raw;
    const reasonEn = m ? m.en : raw;
    return tr(
      `迭代结束${reasonZh ? `（${reasonZh}）` : ''}，接下来生成图表和报告`,
      `Iterations done${reasonEn ? ` (${reasonEn})` : ''}. Figures and report are next.`,
    );
  }
  return null;
}

// —— 实验任务（experiment.*）：observation 的用户可读摘要 ——

/** primary_metric.direction → 大白话方向。 */
function metricDirection(dir: unknown): { zh: string; en: string } | null {
  if (dir === 'maximize') return { zh: '越大越好', en: 'higher is better' };
  if (dir === 'minimize') return { zh: '越小越好', en: 'lower is better' };
  return null;
}

export interface ExperimentStepFriendly {
  /** 一句给用户看的中文小结 */
  text: string;
  /** 附带的条目（文件名 / 指标名），以 tag 展示 */
  items?: string[];
  /** items 前的小标签 */
  itemsLabel?: string;
  /** 影响提示色：出错/降级用警示色 */
  tone?: 'ok' | 'warn';
}

/** 把 experiment.* 动作的 observation 翻译成用户可读摘要；不认识的动作返回 null（回退原始 JSON）。 */
export function experimentStepFriendly(action: string, obs: Record<string, unknown>): ExperimentStepFriendly | null {
  // 任何动作失败时 helm 会把错误写进 observation.error（后端 _guarded）
  if (typeof obs.error === 'string' && obs.error) {
    return {
      text: tr(`出错：${obs.error}`, `Failed: ${obs.error}`),
      tone: 'warn',
    };
  }
  switch (action) {
    case 'experiment.plan': {
      const pm = asObj(obs.primary_metric);
      const name = pm && typeof pm.name === 'string' ? pm.name : '';
      const dir = pm ? metricDirection(pm.direction) : null;
      const metricZh = name ? `主指标 ${name}${dir ? `（${dir.zh}）` : ''}` : '主指标未定';
      const metricEn = name ? `Primary metric ${name}${dir ? ` (${dir.en})` : ''}` : 'No primary metric yet';
      return {
        text: tr(
          `${metricZh} · ${num(obs.hypotheses)} 条假设 · ${num(obs.steps)} 个步骤`,
          `${metricEn} · ${enCount(num(obs.hypotheses), 'hypothesis', 'hypotheses')} · ${enCount(num(obs.steps), 'step')}`,
        ),
      };
    }
    case 'experiment.setup': {
      const files = namesOf(obs.files);
      return {
        text: tr(`生成 ${files.length} 个代码文件`, `Generated ${enCount(files.length, 'code file')}`),
        items: files,
        itemsLabel: tr('文件', 'Files'),
      };
    }
    case 'experiment.smoke': {
      const fixes = num(obs.fixes);
      const passed = num(obs.exit_code) === 0;
      if (!passed) {
        return {
          text: tr('试运行未通过', 'Trial run failed'),
          tone: 'warn',
        };
      }
      return {
        text: fixes > 0
          ? tr(`试运行通过（修正代码 ${fixes} 次）`, `Trial run passed after ${enCount(fixes, 'fix', 'fixes')}`)
          : tr('试运行通过', 'Trial run passed'),
      };
    }
    case 'experiment.run': {
      if (obs.skipped) {
        const reason = typeof obs.stopped_reason === 'string' ? STOPPED_REASON[obs.stopped_reason] : undefined;
        return {
          text: tr(
            `已跳过：迭代已结束${reason ? `（${reason.zh}）` : ''}`,
            `Skipped: iterations already done${reason ? ` (${reason.en})` : ''}`,
          ),
        };
      }
      const seq = num(obs.seq);
      const metrics = namesOf(obs.metric_names);
      const exit = num(obs.exit_code);
      const abnormal = exit !== 0 || (typeof obs.run_status === 'string' && obs.run_status !== 'succeeded');
      const base = abnormal
        ? tr(`第 ${seq} 轮异常退出（退出码 ${exit}）`, `Round ${seq} exited with code ${exit}`)
        : tr(`第 ${seq} 轮完成`, `Round ${seq} done`);
      return {
        text: metrics.length
          ? base + tr(`，得到 ${metrics.length} 项指标`, `, ${enCount(metrics.length, 'metric')}`)
          : base,
        items: metrics,
        itemsLabel: tr('指标', 'Metrics'),
        tone: abnormal ? 'warn' : 'ok',
      };
    }
    case 'experiment.analyze': {
      const seq = num(obs.seq);
      const rounds = num(obs.rounds);
      const roundsZh = rounds > 0 ? `（共 ${rounds} 轮）` : '';
      const roundsEn = rounds > 0 ? ` (${enCount(rounds, 'round')} so far)` : '';
      let decisionZh: string;
      let decisionEn: string;
      switch (obs.decision) {
        case 'improve':
          decisionZh = '继续改进方法';
          decisionEn = 'keep improving the method';
          break;
        case 'debug':
          decisionZh = '先修复报错再重跑';
          decisionEn = 'fix the errors, then rerun';
          break;
        case 'stop':
          decisionZh = '结束迭代';
          decisionEn = 'stop iterating';
          break;
        default:
          decisionZh = '分析完成';
          decisionEn = 'analysis done';
      }
      // 若 observation 带了诊断说明（reflection 字段）则一并展示，读不到就跳过
      const diag = typeof obs.diagnosis === 'string' ? obs.diagnosis
        : typeof obs.observation === 'string' ? obs.observation : '';
      return {
        text: tr(
          `第 ${seq} 轮分析${roundsZh}：${decisionZh}${diag ? `。诊断：${diag}` : ''}`,
          `Round ${seq} analysis${roundsEn}: ${decisionEn}${diag ? `. Diagnosis: ${diag}` : ''}`,
        ),
      };
    }
    case 'experiment.figures': {
      const figures = num(obs.figures);
      const fixes = num(obs.fixes);
      const qcPassed = obs.qc_passed !== false;
      const problem = typeof obs.problem === 'string' ? obs.problem : '';
      const qcZh = qcPassed ? '' : `，质检未通过，改用简化图表${problem ? `：${problem}` : ''}`;
      const qcEn = qcPassed ? '' : `; failed the quality check, so simpler figures were used${problem ? `: ${problem}` : ''}`;
      const fixZh = fixes > 0 ? `，修正 ${fixes} 次` : '';
      const fixEn = fixes > 0 ? ` after ${enCount(fixes, 'fix', 'fixes')}` : '';
      return {
        text: tr(
          `生成 ${figures} 张图表${fixZh}${qcZh}`,
          `Generated ${enCount(figures, 'figure')}${fixEn}${qcEn}`,
        ),
        tone: qcPassed ? 'ok' : 'warn',
      };
    }
    case 'experiment.report': {
      const chars = num(obs.report_chars);
      return {
        text: chars > 0
          ? tr(`报告已生成，约 ${chars} 字`, `Report generated, about ${chars} characters`)
          : tr('报告已生成', 'Report generated'),
      };
    }
    default:
      return null;
  }
}

export function ExperimentStepSummary({ friendly }: { friendly: ExperimentStepFriendly }) {
  const warn = friendly.tone === 'warn';
  return (
    <div
      style={{
        marginTop: 10,
        padding: '9px 12px',
        background: 'var(--surface-2)',
        borderRadius: 9,
        fontSize: 13,
        lineHeight: 1.6,
        color: warn ? 'var(--warn-tx)' : 'var(--text)',
      }}
    >
      {friendly.text}
      {friendly.items && friendly.items.length > 0 && (
        <div className="col" style={{ gap: 6, marginTop: 8 }}>
          {friendly.itemsLabel && (
            <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{friendly.itemsLabel}</span>
          )}
          <div className="row gap6 wrap">
            {friendly.items.map((name) => (
              <span key={name} className="tag mono" style={{ fontSize: 11 }}>
                {name}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// —— 尝试记录（attempts 归档）——

export function AttemptsBlock({ attempts }: { attempts: VoyageStepAttempt[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: 8 }}>
      <button
        className="row gap6"
        onClick={() => setOpen(!open)}
        style={{ border: 'none', background: 'transparent', cursor: 'pointer', padding: 0, fontSize: 12, fontWeight: 600, color: 'var(--text-3)' }}
      >
        <Icon name="chevDown" size={12} style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />
        {open ? tr('收起尝试记录', 'Hide run history') : tr(`查看 ${attempts.length} 次尝试记录`, `Show ${attempts.length} runs`)}
      </button>
      {open && (
        <div className="col" style={{ gap: 6, marginTop: 6 }}>
          {attempts.map((a) => (
            <div key={a.attempt} style={{ padding: '7px 12px', background: 'var(--surface-2)', borderRadius: 8, fontSize: 12, lineHeight: 1.6 }}>
              <div className="row gap8" style={{ flexWrap: 'wrap' }}>
                <span className="mono" style={{ fontWeight: 600 }}>#{a.attempt}</span>
                {a.started_at && (
                  <span className="mono muted" style={{ fontSize: 11 }}>
                    {fmtTime(a.started_at)}
                    {a.finished_at ? ` – ${fmtTime(a.finished_at)} · ${fmtDuration(a.started_at, a.finished_at)}` : ''}
                  </span>
                )}
                {a.verdict && (
                  <span style={{ marginLeft: 'auto' }}>
                    <StatusDot
                      tone={a.verdict.passed ? 'ok' : 'err'}
                      label={a.verdict.passed ? tr('通过', 'Passed') : tr('未通过', 'Failed')}
                    />
                  </span>
                )}
              </div>
              {a.verdict?.reason && (
                <div style={{ marginTop: 3, color: a.verdict.passed ? 'var(--text-3)' : 'var(--danger-tx)' }}>
                  {a.verdict.reason}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// —— 步骤卡 ——

/** 需审批的类型 → 大白话（未知 kind 原样展示）。 */
export const GATE_KIND: Record<string, { zh: string; en: string }> = {
  compute_budget: { zh: '算力预算', en: 'compute budget' },
  experiment_pivot: { zh: '方法调整', en: 'method pivot' },
};

export function ObservationBlock({ observation, compact }: { observation: unknown; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  if (observation === null || observation === undefined) return null;
  const text = typeof observation === 'string' ? observation : JSON.stringify(observation, null, 2);
  const preview = text.length > 160 ? `${text.slice(0, 160)}…` : text;
  return (
    <div style={{ marginTop: 10 }}>
      <button
        className="row gap6"
        onClick={() => setOpen(!open)}
        style={{ border: 'none', background: 'transparent', cursor: 'pointer', padding: 0, fontSize: 12, fontWeight: 600, color: 'var(--text-3)' }}
      >
        <Icon name="chevDown" size={12} style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />
        {compact ? tr('原始数据', 'Raw data') : tr('运行结果数据', 'Result data')}
      </button>
      {/* 有可读摘要（compact）时原始 JSON 只在展开后出现 */}
      {(open || !compact) && (
        <div className="codeblock scroll" style={{ fontSize: 11, marginTop: 6, maxHeight: open ? 400 : 'none', overflowY: 'auto', whiteSpace: 'pre-wrap' }}>
          {open ? text : preview}
        </div>
      )}
    </div>
  );
}

function FailureEvidenceCard({ failure }: { failure: Record<string, unknown> }) {
  const stdout = typeof failure.stdout_tail === 'string' ? failure.stdout_tail : '';
  const stderr = typeof failure.stderr_tail === 'string' ? failure.stderr_tail : '';
  const raw = [stderr && `stderr:\n${stderr}`, stdout && `stdout:\n${stdout}`].filter(Boolean).join('\n\n');
  return (
    <div style={{ marginTop: 10, padding: '10px 12px', borderRadius: 8, background: 'var(--danger-bg)', color: 'var(--danger-tx)' }}>
      <div style={{ fontWeight: 600, overflowWrap: 'anywhere' }}>
        {failure.phase && failure.phase !== 'unknown' ? `${String(failure.phase)} · ` : ''}
        {String(failure.message ?? failure.exception_type ?? tr('命令执行失败', 'Command failed'))}
      </div>
      <div className="mono" style={{ marginTop: 5, fontSize: 11, overflowWrap: 'anywhere' }}>
        {failure.command_display ? `${tr('命令', 'Command')}: ${String(failure.command_display)}` : ''}
        {failure.exit_status !== null && failure.exit_status !== undefined
          ? ` · ${tr('退出码', 'exit code')} ${String(failure.exit_status)}`
          : ''}
        {failure.elapsed_seconds !== null && failure.elapsed_seconds !== undefined
          ? ` · ${Number(failure.elapsed_seconds).toFixed(1)}s`
          : ''}
      </div>
      {raw && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12 }}>
            {tr('查看输出末尾', 'Show last output')}
          </summary>
          <pre style={{ margin: '6px 0 0', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 11 }}>
            {raw}
          </pre>
        </details>
      )}
    </div>
  );
}

export function StepCard({ step, planEvents }: { step: VoyageStepRead; planEvents: VoyagePlanEvent[] }) {
  const obs = asObj(step.observation);
  const failure = obs ? asObj(obs.failure) : null;
  const friendly = obs ? wikiStepFriendly(step.action, obs) : null;
  const expFriendly = obs && step.action.startsWith('experiment.') ? experimentStepFriendly(step.action, obs) : null;
  const obsolete = step.status === 'obsolete';
  const planIter = step.provenance?.plan_iteration ?? 0;
  const gateKind = step.requires_gate ? GATE_KIND[step.requires_gate] : null;
  const signal = obs ? asObj(obs.plan_signal) : null;
  const signalText = signal ? planSignalText(signal) : null;
  const attempts = step.attempts ?? [];
  return (
    <div className="card" style={{ padding: '14px 16px', opacity: obsolete ? 0.55 : 1 }}>
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        <span title={step.action} style={{ fontSize: 13, fontWeight: 600, textDecoration: obsolete ? 'line-through' : 'none' }}>
          {step.title}
        </span>
        {planIter > 0 && (
          <span
            style={{ fontSize: 12, color: 'var(--text-3)' }}
            title={tr('不在最初的计划里，调整计划时加入', 'Not in the original plan; added when the plan changed')}
          >
            {tr(`第 ${planIter} 次调整加入`, `Added in revision ${planIter}`)}
          </span>
        )}
        {step.requires_gate && (
          <span
            className="pill sm"
            style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}
            title={tr('到这一步会暂停，等你批准后继续', 'Pauses here until you approve')}
          >
            <Icon name="gate" size={11} />
            {tr(`需审批：${gateKind ? gateKind.zh : step.requires_gate}`, `Needs approval: ${gateKind ? gateKind.en : step.requires_gate}`)}
          </span>
        )}
        {step.attempt > 1 && (
          <span style={{ fontSize: 12, color: 'var(--warn-tx)' }} title={tr('出错后已自动重试', 'Retried after an error')}>
            {tr(`第 ${step.attempt} 次尝试`, `Attempt ${step.attempt}`)}
          </span>
        )}
        <div style={{ marginLeft: 'auto' }}>
          <TaskStatus status={step.status} />
        </div>
      </div>
      <div className="row gap10" style={{ marginTop: 8, flexWrap: 'wrap' }}>
        {step.verdict && !(step.verdict.passed && step.status === 'passed') && (
          <StatusDot
            tone={step.verdict.passed ? 'ok' : 'err'}
            label={step.verdict.passed ? tr('校验通过', 'Check passed') : tr('校验未通过', 'Check failed')}
            title={step.verdict.reason}
          />
        )}
        {(stepTokenCount(step.tokens) ?? 0) > 0 && (
          <span className="mono muted" style={{ fontSize: 11 }}>
            <Icon name="cpu" size={11} style={{ display: 'inline-block', verticalAlign: '-1.5px', marginRight: 4 }} />
            {fmtTokens(stepTokenCount(step.tokens))} tokens
          </span>
        )}
        {step.started_at && (
          <span className="mono muted" style={{ fontSize: 11 }}>
            {fmtTime(step.started_at)} · {step.finished_at ? `${tr('耗时', 'took')} ${fmtDuration(step.started_at, step.finished_at)}` : tr('进行中', 'in progress')}
          </span>
        )}
      </div>
      {/* 判定理由：通过与否都展示（通过时用弱色）。
          reason 是后端原样透传的错误文本，可能整段是一条没有断点的长 URL
          （如 arXiv 检索失败时的 query 串）。overflow-wrap 默认 normal 时这种
          token 会直接溢出盒子而不是换行，把卡片撑破，故显式允许任意位置断行。 */}
      {step.verdict?.reason && !step.verdict.passed && !failure && (
        <div
          style={{
            marginTop: 8,
            fontSize: 12,
            color: step.verdict.passed ? 'var(--text-3)' : 'var(--danger-tx)',
            lineHeight: 1.5,
            overflowWrap: 'anywhere',
          }}
        >
          {tr('理由：', 'Reason: ')}
          {step.verdict.reason}
        </div>
      )}
      {/* 作废步骤：补一行为什么被作废 */}
      {obsolete && (
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-3)', lineHeight: 1.5 }}>
          {obsoleteReasonOf(step, planEvents)}
        </div>
      )}
      {/* 分析步骤的因果摘要：这一步的结论如何改变了后续计划 */}
      {signalText && (
        <div
          className="row gap6"
          style={{ marginTop: 10, padding: '8px 12px', background: 'var(--accent-soft)', color: 'var(--accent-text)', borderRadius: 8, fontSize: 13, lineHeight: 1.5, alignItems: 'flex-start' }}
        >
          <Icon name="compass" size={13} style={{ flexShrink: 0, marginTop: 2 }} />
          {/* 同上：signalText 会拼进 observation.error 的原文 */}
          <span style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{signalText}</span>
        </div>
      )}
      {friendly && <WikiStepSummary friendly={friendly} />}
      {expFriendly && !(failure && typeof obs?.error === 'string') && <ExperimentStepSummary friendly={expFriendly} />}
      {failure && <FailureEvidenceCard failure={failure} />}
      {step.acceptance && <AcceptanceBlock acceptance={step.acceptance} />}
      {attempts.length > 1 && <AttemptsBlock attempts={attempts} />}
      <ObservationBlock observation={step.observation} compact={!!friendly || !!expFriendly} />
    </div>
  );
}
