import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { FormField } from '../../components/ui/FormField';
import { Segmented } from '../../components/ui/Segmented';
import { AccordionSection } from '../../components/ui/Accordion';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { api, type AnchorPaper, type ProjectDefinition, type RubricDimension } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   /projects/new — 创建页，两个可自由切换的标签页。
   基本信息（唯一必填）：名称 + 一句话定义 + include 关键词；
   高级设置（全部可选）：折叠分区编辑 definition 其余字段。
   底部操作栏两页共用：「AI 帮我设置」按基本信息草拟高级设置
   （只填空白分区，不覆盖已填内容，可反复点）+「创建方向」。
   ============================================================ */

const TABS = [
  { zh: '基本信息', en: 'Basics' },
  { zh: '高级设置', en: 'Advanced · optional' },
] as const;

/** 常用 arXiv 分类快捷多选（第 1 步）。 */
const QUICK_CATEGORIES = ['cs.CL', 'cs.AI', 'cs.LG', 'cs.CV', 'cs.MA', 'stat.ML'];

const CADENCES = [
  { v: 'daily', zh: '每日', en: 'Daily' },
  { v: 'weekly', zh: '每周', en: 'Weekly' },
  { v: 'manual', zh: '手动', en: 'Manual' },
] as const;

const DEFAULT_RUBRIC: RubricRow[] = [
  { name: '新颖性', description: '与已有工作的差异化程度', weight: '1.0' },
  { name: '可行性', description: '现有资源下能否完成实验验证', weight: '1.0' },
];

type SectionKey = 'goals' | 'questions' | 'rubric' | 'anchors' | 'synonyms' | 'cadence';

const SECTIONS: { key: SectionKey; zh: string; en: string }[] = [
  { key: 'goals', zh: '目标与范围', en: 'Goals & Scope' },
  { key: 'questions', zh: '研究问题', en: 'Research questions' },
  { key: 'rubric', zh: '打分 Rubric', en: 'Scoring rubric' },
  { key: 'anchors', zh: '锚点论文', en: 'Anchor papers' },
  { key: 'synonyms', zh: '同义词映射', en: 'Synonyms' },
  { key: 'cadence', zh: '运行节奏', en: 'Cadence' },
];

interface RubricRow {
  name: string;
  description: string;
  weight: string; // 输入态为字符串，提交时 parse
}

interface SynonymRow {
  term: string;
  syns: string; // 逗号分隔
}

/* 可编辑列表行的稳定 key：index 作 key 时增删行会让受控输入串位 */
let rowSeq = 0;
type Keyed<T> = T & { _k: number };
function withKey<T extends object>(row: T): Keyed<T> {
  rowSeq += 1;
  return { ...row, _k: rowSeq };
}

/** 字符串列表编辑器（回车/按钮添加，逐条删除）。 */
function ListEditor({
  items,
  onChange,
  placeholder,
}: {
  items: string[];
  onChange: (items: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState('');
  function add() {
    const v = draft.trim();
    if (!v) return;
    if (!items.includes(v)) onChange([...items, v]);
    setDraft('');
  }
  return (
    <div className="col gap8">
      {items.map((it, i) => (
        <div key={i} className="row gap8 list-row">
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', width: 18, textAlign: 'right', flexShrink: 0 }}>
            {i + 1}.
          </span>
          <span style={{ flex: 1, fontSize: 13, lineHeight: 1.45 }}>{it}</span>
          <button className="icon-btn" style={{ width: 24, height: 24, border: 'none', background: 'transparent' }}
            onClick={() => onChange(items.filter((_, j) => j !== i))} title={tr('删除', 'Remove')}>
            <Icon name="x" size={13} />
          </button>
        </div>
      ))}
      <div className="row gap8">
        <input
          className="input"
          style={{ flex: 1 }}
          placeholder={placeholder}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add();
            }
          }}
        />
        <button className="btn btn-soft sm" style={{ height: 38 }} onClick={add}>
          <Icon name="plus" size={13} />
          {tr('添加', 'Add')}
        </button>
      </div>
    </div>
  );
}

/** chips 输入：回车添加，点击 chip 移除。 */
function ChipsInput({
  items,
  onChange,
  placeholder,
}: {
  items: string[];
  onChange: (items: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState('');
  function add() {
    const v = draft.trim();
    if (!v) return;
    if (!items.includes(v)) onChange([...items, v]);
    setDraft('');
  }
  return (
    <div className="row gap6 wrap" style={{ alignItems: 'center' }}>
      {items.map((t) => (
        <button key={t} type="button" className="chip on" onClick={() => onChange(items.filter((x) => x !== t))}
          title={tr('点击移除', 'Click to remove')}>
          {t} ×
        </button>
      ))}
      <input
        className="input"
        style={{ width: 220 }}
        placeholder={placeholder}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={add}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            add();
          }
        }}
      />
    </div>
  );
}

export function ProjectWizardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setCurrentProjectId } = useProject();

  const [tab, setTab] = useState<0 | 1>(0);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [drafting, setDrafting] = useState(false);

  // —— 第 1 步（必填） ——
  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  const [includeTerms, setIncludeTerms] = useState<string[]>([]);
  const [categories, setCategories] = useState<string[]>(['cs.CL', 'cs.AI']);

  // —— 第 2 步（全部可选） ——
  const [goals, setGoals] = useState<string[]>([]);
  const [inScope, setInScope] = useState<string[]>([]);
  const [outScope, setOutScope] = useState<string[]>([]);
  const [questions, setQuestions] = useState<string[]>([]);
  const [rubric, setRubric] = useState<Keyed<RubricRow>[]>(() => DEFAULT_RUBRIC.map(withKey));
  const [anchors, setAnchors] = useState<Keyed<AnchorPaper>[]>([]);
  const [synonyms, setSynonyms] = useState<Keyed<SynonymRow>[]>([]);
  const [cadence, setCadence] = useState<string>('daily');
  const [open, setOpen] = useState<Record<SectionKey, boolean>>({
    goals: false, questions: false, rubric: false, anchors: false, synonyms: false, cadence: false,
  });
  const [aiFilled, setAiFilled] = useState<Set<SectionKey>>(new Set());

  function toggleSection(k: SectionKey) {
    setOpen((o) => ({ ...o, [k]: !o[k] }));
  }

  function toggleCategory(c: string) {
    setCategories((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]));
  }

  function validateBasics(): string | null {
    if (!name.trim()) return tr('请填写课题名称', 'Please enter a topic name');
    if (!statement.trim()) return tr('请用一句话定义这个课题', 'Please define this topic in one sentence');
    if (includeTerms.length === 0) return tr('至少添加 1 个 include 关键词（标题/摘要命中才进入相关性判定）', 'Add at least 1 include term (papers must match it in title/abstract to be scored)');
    return null;
  }

  function buildDefinition(): ProjectDefinition {
    const def: ProjectDefinition = { statement: statement.trim() };
    if (goals.length) def.goals = goals;
    if (inScope.length) def.in_scope = inScope;
    if (outScope.length) def.out_of_scope = outScope;
    if (questions.length) def.questions = questions;
    const dims: RubricDimension[] = rubric
      .filter((r) => r.name.trim())
      .map((r) => ({ name: r.name.trim(), description: r.description.trim(), weight: Number(r.weight) || 1 }));
    if (dims.length) def.rubric = dims;
    const papers = anchors.filter((a) => a.title.trim());
    if (papers.length) {
      def.anchor_papers = papers.map((a) => ({
        title: a.title.trim(),
        ...(a.arxiv_id?.trim() ? { arxiv_id: a.arxiv_id.trim() } : {}),
        ...(a.url?.trim() ? { url: a.url.trim() } : {}),
        ...(a.reason?.trim() ? { reason: a.reason.trim() } : {}),
      }));
    }
    const syn: Record<string, string[]> = {};
    for (const s of synonyms) {
      const term = s.term.trim();
      const list = s.syns.split(/[,，、]/).map((x) => x.trim()).filter(Boolean);
      if (term && list.length) syn[term] = list;
    }
    def.keywords = {
      ...(categories.length ? { arxiv_categories: categories } : {}),
      include: includeTerms,
      ...(Object.keys(syn).length ? { synonyms: syn } : {}),
    };
    def.cadence = cadence;
    return def;
  }

  /** 把 AI 草稿写入第 2 步各分区状态；返回被填入的分区（用于默认展开）。 */
  function applyDraft(d: ProjectDefinition): Set<SectionKey> {
    const filled = new Set<SectionKey>();
    // 只填当前为空的分区：用户已填写的内容一律不覆盖
    if (d.goals?.length || d.in_scope?.length || d.out_of_scope?.length) {
      if (d.goals?.length && goals.length === 0) setGoals(d.goals);
      if (d.in_scope?.length && inScope.length === 0) setInScope(d.in_scope);
      if (d.out_of_scope?.length && outScope.length === 0) setOutScope(d.out_of_scope);
      filled.add('goals');
    }
    if (d.questions?.length && questions.length === 0) {
      setQuestions(d.questions);
      filled.add('questions');
    }
    if (d.rubric?.length && JSON.stringify(rubric.map(({ _k, ...r }) => r)) === JSON.stringify(DEFAULT_RUBRIC)) {
      setRubric(d.rubric.map((r) => withKey({ name: r.name, description: r.description ?? '', weight: String(r.weight ?? 1) })));
      filled.add('rubric');
    }
    if (d.anchor_papers?.length && anchors.length === 0) {
      setAnchors(d.anchor_papers.map(withKey));
      filled.add('anchors');
    }
    if (d.keywords) {
      const syn = d.keywords.synonyms;
      if (syn && Object.keys(syn).length && synonyms.length === 0) {
        setSynonyms(Object.entries(syn).map(([term, list]) => withKey({ term, syns: list.join(', ') })));
        filled.add('synonyms');
      }
      // AI 建议的分类/关键词与用户已选合并（去重）
      if (d.keywords.arxiv_categories?.length) {
        setCategories((prev) => [...new Set([...prev, ...d.keywords!.arxiv_categories!])]);
      }
      if (d.keywords.include?.length) {
        setIncludeTerms((prev) => [...new Set([...prev, ...d.keywords!.include!])]);
      }
    }
    if (d.cadence) {
      setCadence(d.cadence);
      filled.add('cadence');
    }
    // 反复点「AI 帮我设置」时：徽标与展开状态做并集，不清掉上一轮的
    setAiFilled((prev) => new Set([...prev, ...filled]));
    setOpen((o) => ({
      goals: o.goals || filled.has('goals'),
      questions: o.questions || filled.has('questions'),
      rubric: o.rubric || filled.has('rubric'),
      anchors: o.anchors || filled.has('anchors'),
      synonyms: o.synonyms || filled.has('synonyms'),
      cadence: o.cadence || filled.has('cadence'),
    }));
    return filled;
  }

  /** 「AI 帮我设置」：按基本信息草拟 definition → 填入高级设置空白分区并切过去。
      不覆盖已填内容，可反复点（每次只补当前仍为空的分区）。 */
  async function aiDraft() {
    const err = validateBasics();
    setFormError(err);
    if (err) {
      setTab(0); // 基本信息没填全：切回去看错误提示
      return;
    }
    setDrafting(true);
    try {
      const res = await api.draftDefinition({
        statement: statement.trim(),
        name: name.trim(),
        keywords_include: includeTerms,
      });
      const filled = applyDraft(res.definition ?? {});
      if (res.source === 'fallback') {
        toast(tr('AI 未配置，已用默认模板', 'AI not configured — used the default template'), 'info');
      } else if (filled.size === 0) {
        toast(tr('高级设置各分区都已有内容，没有需要 AI 补的空白', 'All advanced sections already have content — nothing for AI to fill'), 'info');
      } else {
        toast(tr('AI 草稿已生成，可在下方微调', 'AI draft ready — tweak it below'), 'ok');
      }
      setTab(1);
    } catch (e) {
      // 端点未就绪/调用失败：优雅降级，仍可手动填写或直接创建
      toast(tr(`AI 设置失败：${e instanceof Error ? e.message : String(e)}，可手动填写高级设置`, `AI setup failed: ${e instanceof Error ? e.message : String(e)} — you can fill the advanced settings manually`), 'error');
      setTab(1);
    } finally {
      setDrafting(false);
    }
  }

  /** 创建方向（第 1 步「直接创建」与第 2 步「创建方向」共用）。 */
  async function create() {
    const err = validateBasics();
    if (err) {
      setFormError(err);
      setTab(0);
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const created = await api.createProject({ name: name.trim(), definition: buildDefinition() });
      await queryClient.invalidateQueries({ queryKey: ['projects'] });
      setCurrentProjectId(created.id);
      toast(tr('课题已创建', 'Topic created'), 'ok');
      navigate(`/projects/${created.id}`);
    } catch (e) {
      toast(`${tr('创建失败：', 'Create failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    } finally {
      setSubmitting(false);
    }
  }

  const busy = submitting || drafting;

  return (
    <div className="page fadeup" style={{ maxWidth: 860 }}>
      <PageHead
        eyebrow="Polaris · Topics"
        title={tr('新建课题', 'New topic')}
        sub={tr('填三项必填信息即可创建；高级设置可交给 AI 草拟后再微调。', 'Fill in three required fields to create; let AI draft the advanced settings, then tweak.')}
      />

      {/* 标签页切换：两页随时可来回切，填写内容都保留 */}
      <div className="wiz-steps">
        {TABS.map((s, i) => (
          <div
            key={s.en}
            className={'wiz-step' + (i === tab ? ' on' : '')}
            onClick={() => setTab(i as 0 | 1)}
            style={{ cursor: 'pointer' }}
          >
            <span className="wiz-no mono">{i + 1}</span>
            <span className="wiz-label">{tr(s.zh, s.en)}</span>
          </div>
        ))}
      </div>

      {tab === 0 && (
        <>
          <div className="card card-pad">
            <FormField label={tr('课题名称', 'Name')} hint={tr('将显示在侧栏与课题列表中', 'Shown in the sidebar and topic list')}>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)}
                placeholder={tr('如：LLM 自主科研智能体', 'e.g. LLM autonomous research agents')} />
            </FormField>
            <FormField label={tr('一句话定义', 'Statement')} hint={tr('用一句话说清这个课题研究什么、为什么重要', 'One sentence on what this topic studies and why it matters')}>
              <textarea className="textarea" rows={3} value={statement} onChange={(e) => setStatement(e.target.value)}
                placeholder={tr('如：让 LLM agent 端到端完成从文献调研到论文的研究方法与系统', 'e.g. Methods and systems for LLM agents to go end-to-end from literature survey to paper')} />
            </FormField>
            <FormField label={tr('Include 关键词', 'Include terms')} hint={tr('标题/摘要命中这些词才进入相关性判定；回车添加，点击移除（至少 1 个）', 'Papers must match these in title/abstract to be scored; Enter to add, click to remove (at least 1)')}>
              <ChipsInput items={includeTerms} onChange={setIncludeTerms} placeholder={tr('如 agent、tool use，回车添加', 'e.g. agent, tool use — Enter to add')} />
            </FormField>
            <FormField label={tr('arXiv 分类', 'arXiv categories')} hint={tr('常用分类快捷多选；更多分类可在创建后编辑', 'Quick-pick common categories; edit more after creating')}>
              <div className="row gap6 wrap">
                {QUICK_CATEGORIES.map((c) => (
                  <button key={c} type="button" className={'chip mono' + (categories.includes(c) ? ' on' : '')}
                    onClick={() => toggleCategory(c)}>
                    {c}
                  </button>
                ))}
                {categories.filter((c) => !QUICK_CATEGORIES.includes(c)).map((c) => (
                  <button key={c} type="button" className="chip mono on" onClick={() => toggleCategory(c)} title={tr('点击移除', 'Click to remove')}>
                    {c} ×
                  </button>
                ))}
              </div>
            </FormField>
            {formError && <div className="field-error" style={{ marginTop: 4 }}>{formError}</div>}
          </div>
        </>
      )}

      {tab === 1 && (
        <>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', margin: '0 2px 12px', lineHeight: 1.6 }}>
            {tr('以下内容全部可选，可直接创建课题。', 'Everything below is optional — you can create the topic right away.')}
            {aiFilled.size > 0 && tr(' AI 已预填部分分区，请确认或微调。', ' AI pre-filled some sections; review or tweak them.')}
          </div>
          <div className="col gap10">
            {SECTIONS.map((s) => (
              <AccordionSection key={s.key} title={tr(s.zh, s.en)} open={open[s.key]}
                onToggle={() => toggleSection(s.key)}
                badge={aiFilled.has(s.key) ? tr('AI 草稿', 'AI draft') : undefined}>
                {s.key === 'goals' && (
                  <>
                    <FormField label={tr('研究目标', 'Goals')} hint={tr('这个课题希望达成什么', 'What this topic aims to achieve')}>
                      <ListEditor items={goals} onChange={setGoals} placeholder={tr('输入一个目标后回车', 'Type a goal, then press Enter')} />
                    </FormField>
                    <div className="row gap16" style={{ alignItems: 'flex-start' }}>
                      <FormField label={tr('范围内', 'In scope')} style={{ flex: 1 }}>
                        <ListEditor items={inScope} onChange={setInScope} placeholder={tr('明确包含的主题', 'Topics explicitly included')} />
                      </FormField>
                      <FormField label={tr('范围外', 'Out of scope')} style={{ flex: 1 }}>
                        <ListEditor items={outScope} onChange={setOutScope} placeholder={tr('明确排除的主题', 'Topics explicitly excluded')} />
                      </FormField>
                    </div>
                  </>
                )}

                {s.key === 'questions' && (
                  <FormField label={tr('具体研究问题', 'Research questions')} hint={tr('建议 3–5 个可验证的具体问题', '3–5 specific, verifiable questions recommended')}>
                    <ListEditor items={questions} onChange={setQuestions} placeholder={tr('如：citation-graph 重排序能否降低新颖性幻觉？', 'e.g. Can citation-graph reranking reduce novelty hallucination?')} />
                  </FormField>
                )}

                {s.key === 'rubric' && (
                  <>
                    <div className="field-hint" style={{ marginBottom: 10 }}>
                      {tr('论文相关性 / idea 质量打分标准，可增删维度与权重', 'Scoring criteria for paper relevance / idea quality; add or remove dimensions and weights')}
                    </div>
                    <div className="col gap10">
                      {rubric.map((r, i) => (
                        <div key={r._k} className="row gap8" style={{ alignItems: 'flex-start' }}>
                          <input className="input" style={{ width: 130 }} placeholder={tr('维度名', 'Dimension')}
                            value={r.name}
                            onChange={(e) => setRubric(rubric.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                          <input className="input" style={{ flex: 1 }} placeholder={tr('打分标准描述', 'Scoring criteria')}
                            value={r.description}
                            onChange={(e) => setRubric(rubric.map((x, j) => (j === i ? { ...x, description: e.target.value } : x)))} />
                          <input className="input mono" style={{ width: 72 }} placeholder={tr('权重', 'Weight')} inputMode="decimal"
                            value={r.weight}
                            onChange={(e) => setRubric(rubric.map((x, j) => (j === i ? { ...x, weight: e.target.value } : x)))} />
                          <button className="icon-btn" style={{ height: 38 }} title={tr('删除维度', 'Remove dimension')}
                            onClick={() => setRubric(rubric.filter((_, j) => j !== i))}>
                            <Icon name="trash" size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                    <button className="btn btn-soft sm" style={{ marginTop: 12 }}
                      onClick={() => setRubric([...rubric, withKey({ name: '', description: '', weight: '1.0' })])}>
                      <Icon name="plus" size={13} />
                      {tr('添加维度', 'Add dimension')}
                    </button>
                  </>
                )}

                {s.key === 'anchors' && (
                  <>
                    <div className="field-hint" style={{ marginBottom: 10 }}>
                      {tr('体现课题研究品味的代表作（可留空）', 'Representative papers that capture the taste of this topic (optional)')}
                    </div>
                    <div className="col gap12">
                      {anchors.map((a, i) => (
                        <div key={a._k} className="card" style={{ padding: '12px 14px', background: 'var(--surface-2)' }}>
                          <div className="row gap8" style={{ marginBottom: 8 }}>
                            <input className="input" style={{ flex: 1 }} placeholder={tr('论文标题（必填）', 'Paper title (required)')}
                              value={a.title}
                              onChange={(e) => setAnchors(anchors.map((x, j) => (j === i ? { ...x, title: e.target.value } : x)))} />
                            <button className="icon-btn" style={{ height: 38 }} title={tr('删除', 'Remove')}
                              onClick={() => setAnchors(anchors.filter((_, j) => j !== i))}>
                              <Icon name="trash" size={14} />
                            </button>
                          </div>
                          <div className="row gap8" style={{ marginBottom: 8 }}>
                            <input className="input mono" style={{ width: 160 }} placeholder="arxiv_id"
                              value={a.arxiv_id ?? ''}
                              onChange={(e) => setAnchors(anchors.map((x, j) => (j === i ? { ...x, arxiv_id: e.target.value } : x)))} />
                            <input className="input" style={{ flex: 1 }} placeholder="url"
                              value={a.url ?? ''}
                              onChange={(e) => setAnchors(anchors.map((x, j) => (j === i ? { ...x, url: e.target.value } : x)))} />
                          </div>
                          <input className="input" style={{ width: '100%' }} placeholder={tr('为什么它是这个课题的锚点', 'Why this paper anchors the topic')}
                            value={a.reason ?? ''}
                            onChange={(e) => setAnchors(anchors.map((x, j) => (j === i ? { ...x, reason: e.target.value } : x)))} />
                        </div>
                      ))}
                    </div>
                    <button className="btn btn-soft sm" style={{ marginTop: 12 }}
                      onClick={() => setAnchors([...anchors, withKey({ title: '', arxiv_id: '', url: '', reason: '' })])}>
                      <Icon name="plus" size={13} />
                      {tr('添加论文', 'Add paper')}
                    </button>
                  </>
                )}

                {s.key === 'synonyms' && (
                  <>
                    <div className="field-hint" style={{ marginBottom: 10 }}>
                      {tr('术语 → 同义词（逗号分隔），提升关键词召回', 'Term → synonyms (comma-separated) to improve keyword recall')}
                    </div>
                    <div className="col gap8">
                      {synonyms.map((sy, i) => (
                        <div key={sy._k} className="row gap8">
                          <input className="input" style={{ width: 180 }} placeholder={tr('术语', 'Term')}
                            value={sy.term}
                            onChange={(e) => setSynonyms(synonyms.map((x, j) => (j === i ? { ...x, term: e.target.value } : x)))} />
                          <span style={{ color: 'var(--text-4)' }}>→</span>
                          <input className="input" style={{ flex: 1 }} placeholder={tr('同义词1, 同义词2, …', 'synonym 1, synonym 2, …')}
                            value={sy.syns}
                            onChange={(e) => setSynonyms(synonyms.map((x, j) => (j === i ? { ...x, syns: e.target.value } : x)))} />
                          <button className="icon-btn" style={{ height: 38 }} title={tr('删除', 'Remove')}
                            onClick={() => setSynonyms(synonyms.filter((_, j) => j !== i))}>
                            <Icon name="trash" size={14} />
                          </button>
                        </div>
                      ))}
                      <button className="btn btn-soft sm" style={{ alignSelf: 'flex-start' }}
                        onClick={() => setSynonyms([...synonyms, withKey({ term: '', syns: '' })])}>
                        <Icon name="plus" size={13} />
                        {tr('添加映射', 'Add mapping')}
                      </button>
                    </div>
                  </>
                )}

                {s.key === 'cadence' && (
                  <FormField label={tr('运行节奏', 'Cadence')} hint={tr('文献追踪等自动任务的运行频率', 'How often automatic tasks like literature tracking run')}>
                    <div>
                      <Segmented options={CADENCES.map((c) => ({ v: c.v, label: tr(c.zh, c.en) }))}
                        value={cadence as (typeof CADENCES)[number]['v']}
                        onChange={(v) => setCadence(v)} />
                    </div>
                  </FormField>
                )}
              </AccordionSection>
            ))}
          </div>

        </>
      )}

      {/* 底部操作栏：两个标签页共用 */}
      <div className="row gap10" style={{ marginTop: 18 }}>
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
          {tr('AI 按基本信息草拟高级设置，只补空白分区，不覆盖你已填的内容', 'AI drafts the advanced settings from the basics — it only fills blank sections and never overwrites your input')}
        </span>
        <div style={{ flex: 1 }} />
        <button className="btn btn-soft" onClick={() => void aiDraft()} disabled={busy}>
          <Icon name="sparkle" size={14} />
          {drafting ? tr('AI 草拟中…', 'AI drafting…') : tr('AI 帮我设置', 'Set up with AI')}
        </button>
        <button className="btn btn-primary" onClick={() => void create()} disabled={busy}>
          <Icon name="check" size={14} />
          {submitting ? tr('创建中…', 'Creating…') : tr('创建课题', 'Create topic')}
        </button>
      </div>
    </div>
  );
}
