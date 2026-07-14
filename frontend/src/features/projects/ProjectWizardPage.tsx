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

/* ============================================================
   /projects/new — 两步创建向导。
   第 1 步（唯一必填）：名称 + 一句话定义 + include 关键词；
   可「直接创建」，或让 AI 草拟高级设置后进入第 2 步微调。
   第 2 步（全部可选）：折叠分区编辑 definition 其余字段。
   ============================================================ */

const STEPS = [
  { zh: '基本信息', en: 'Basics' },
  { zh: '高级设置', en: 'Advanced · optional' },
] as const;

/** 常用 arXiv 分类快捷多选（第 1 步）。 */
const QUICK_CATEGORIES = ['cs.CL', 'cs.AI', 'cs.LG', 'cs.CV', 'cs.MA', 'stat.ML'];

const CADENCES = [
  { v: 'daily', label: '每日 daily' },
  { v: 'weekly', label: '每周 weekly' },
  { v: 'manual', label: '手动 manual' },
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
            onClick={() => onChange(items.filter((_, j) => j !== i))} title="删除">
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
          添加
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
          title="点击移除">
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

  const [step, setStep] = useState<0 | 1>(0);
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
  const [rubric, setRubric] = useState<RubricRow[]>(DEFAULT_RUBRIC);
  const [anchors, setAnchors] = useState<AnchorPaper[]>([]);
  const [synonyms, setSynonyms] = useState<SynonymRow[]>([]);
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
    if (!name.trim()) return '请填写方向名称';
    if (!statement.trim()) return '请用一句话定义这个研究方向';
    if (includeTerms.length === 0) return '至少添加 1 个 include 关键词（标题/摘要命中才进入相关性判定）';
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
    if (d.goals?.length || d.in_scope?.length || d.out_of_scope?.length) {
      if (d.goals?.length) setGoals(d.goals);
      if (d.in_scope?.length) setInScope(d.in_scope);
      if (d.out_of_scope?.length) setOutScope(d.out_of_scope);
      filled.add('goals');
    }
    if (d.questions?.length) {
      setQuestions(d.questions);
      filled.add('questions');
    }
    if (d.rubric?.length) {
      setRubric(d.rubric.map((r) => ({ name: r.name, description: r.description ?? '', weight: String(r.weight ?? 1) })));
      filled.add('rubric');
    }
    if (d.anchor_papers?.length) {
      setAnchors(d.anchor_papers);
      filled.add('anchors');
    }
    if (d.keywords) {
      const syn = d.keywords.synonyms;
      if (syn && Object.keys(syn).length) {
        setSynonyms(Object.entries(syn).map(([term, list]) => ({ term, syns: list.join(', ') })));
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
    setAiFilled(filled);
    setOpen({
      goals: filled.has('goals'),
      questions: filled.has('questions'),
      rubric: filled.has('rubric'),
      anchors: filled.has('anchors'),
      synonyms: filled.has('synonyms'),
      cadence: filled.has('cadence'),
    });
    return filled;
  }

  /** 「AI 补全高级设置」：草拟 definition → 填入第 2 步并进入。 */
  async function aiDraft() {
    const err = validateBasics();
    setFormError(err);
    if (err) return;
    setDrafting(true);
    try {
      const res = await api.draftDefinition({
        statement: statement.trim(),
        name: name.trim(),
        keywords_include: includeTerms,
      });
      applyDraft(res.definition ?? {});
      if (res.source === 'fallback') {
        toast('AI 未配置，已用默认模板', 'info');
      } else {
        toast('AI 草稿已生成，可在下方微调', 'ok');
      }
      setStep(1);
    } catch (e) {
      // 端点未就绪/调用失败：优雅降级，仍可手动填写或直接创建
      toast(`AI 补全失败：${e instanceof Error ? e.message : String(e)}，可手动填写高级设置`, 'error');
      setStep(1);
    } finally {
      setDrafting(false);
    }
  }

  /** 创建方向（第 1 步「直接创建」与第 2 步「创建方向」共用）。 */
  async function create() {
    const err = validateBasics();
    if (err) {
      setFormError(err);
      setStep(0);
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const created = await api.createProject({ name: name.trim(), definition: buildDefinition() });
      await queryClient.invalidateQueries({ queryKey: ['projects'] });
      setCurrentProjectId(created.id);
      toast('研究方向已创建', 'ok');
      navigate(`/projects/${created.id}`);
    } catch (e) {
      toast(`创建失败：${e instanceof Error ? e.message : String(e)}`, 'error');
    } finally {
      setSubmitting(false);
    }
  }

  const busy = submitting || drafting;

  return (
    <div className="page fadeup" style={{ maxWidth: 860 }}>
      <PageHead
        eyebrow="Polaris · Directions"
        title="新建研究方向"
        sub="填三项必填信息即可创建；高级设置可交给 AI 草拟后再微调。"
        en="Two-step direction setup"
      />

      {/* 步骤指示器 */}
      <div className="wiz-steps">
        {STEPS.map((s, i) => (
          <div
            key={s.en}
            className={'wiz-step' + (i === step ? ' on' : i < step ? ' done' : '')}
            onClick={() => {
              if (i < step) {
                setFormError(null);
                setStep(0);
              }
            }}
            style={{ cursor: i < step ? 'pointer' : 'default' }}
          >
            <span className="wiz-no mono">{i < step ? <Icon name="check" size={11} /> : i + 1}</span>
            <span className="wiz-label">
              {s.zh}
              <span className="en">{s.en}</span>
            </span>
          </div>
        ))}
      </div>

      {step === 0 && (
        <>
          <div className="card card-pad">
            <FormField label="方向名称" en="Name" hint="将显示在侧栏与项目列表中">
              <input className="input" value={name} onChange={(e) => setName(e.target.value)}
                placeholder="如：LLM 自主科研智能体" />
            </FormField>
            <FormField label="一句话定义" en="Statement" hint="用一句话说清这个方向研究什么、为什么重要">
              <textarea className="textarea" rows={3} value={statement} onChange={(e) => setStatement(e.target.value)}
                placeholder="如：研究让 LLM agent 端到端完成文献调研 → idea → 实验 → 论文的方法与系统" />
            </FormField>
            <FormField label="Include 关键词" en="Include terms" hint="标题/摘要命中这些词才进入相关性判定；回车添加，点击移除（至少 1 个）">
              <ChipsInput items={includeTerms} onChange={setIncludeTerms} placeholder="如 agent、tool use，回车添加" />
            </FormField>
            <FormField label="arXiv 分类" en="Categories" hint="常用分类快捷多选；更多分类可在创建后编辑">
              <div className="row gap6 wrap">
                {QUICK_CATEGORIES.map((c) => (
                  <button key={c} type="button" className={'chip mono' + (categories.includes(c) ? ' on' : '')}
                    onClick={() => toggleCategory(c)}>
                    {c}
                  </button>
                ))}
                {categories.filter((c) => !QUICK_CATEGORIES.includes(c)).map((c) => (
                  <button key={c} type="button" className="chip mono on" onClick={() => toggleCategory(c)} title="点击移除">
                    {c} ×
                  </button>
                ))}
              </div>
            </FormField>
            {formError && <div className="field-error" style={{ marginTop: 4 }}>{formError}</div>}
          </div>

          <div className="row gap10" style={{ marginTop: 18 }}>
            <button className="btn btn-ghost" onClick={() => { setFormError(null); setStep(1); }} disabled={busy}>
              跳过 AI，手动配置
            </button>
            <div style={{ flex: 1 }} />
            <button className="btn btn-soft" onClick={() => void aiDraft()} disabled={busy}>
              <Icon name="sparkle" size={14} />
              {drafting ? 'AI 草拟中…' : 'AI 补全高级设置'}
            </button>
            <button className="btn btn-primary" onClick={() => void create()} disabled={busy}>
              <Icon name="check" size={14} />
              {submitting ? '创建中…' : '直接创建'}
            </button>
          </div>
        </>
      )}

      {step === 1 && (
        <>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', margin: '0 2px 12px', lineHeight: 1.6 }}>
            以下全部可选，可直接点「创建方向」跳过。
            {aiFilled.size > 0 && ' 标有「AI 草稿」的分区已由 AI 预填，请确认或微调。'}
          </div>
          <div className="col gap10">
            {SECTIONS.map((s) => (
              <AccordionSection key={s.key} title={s.zh} en={s.en} open={open[s.key]}
                onToggle={() => toggleSection(s.key)}
                badge={aiFilled.has(s.key) ? 'AI 草稿' : undefined}>
                {s.key === 'goals' && (
                  <>
                    <FormField label="研究目标" en="Goals" hint="这个方向希望达成什么">
                      <ListEditor items={goals} onChange={setGoals} placeholder="输入一个目标后回车" />
                    </FormField>
                    <div className="row gap16" style={{ alignItems: 'flex-start' }}>
                      <FormField label="范围内" en="In scope" style={{ flex: 1 }}>
                        <ListEditor items={inScope} onChange={setInScope} placeholder="明确包含的主题" />
                      </FormField>
                      <FormField label="范围外" en="Out of scope" style={{ flex: 1 }}>
                        <ListEditor items={outScope} onChange={setOutScope} placeholder="明确排除的主题" />
                      </FormField>
                    </div>
                  </>
                )}

                {s.key === 'questions' && (
                  <FormField label="具体研究问题" en="Research questions" hint="建议 3–5 个可验证的具体问题">
                    <ListEditor items={questions} onChange={setQuestions} placeholder="如：citation-graph 重排序能否降低新颖性幻觉？" />
                  </FormField>
                )}

                {s.key === 'rubric' && (
                  <>
                    <div className="field-hint" style={{ marginBottom: 10 }}>
                      论文相关性 / idea 质量打分标准，可增删维度与权重
                    </div>
                    <div className="col gap10">
                      {rubric.map((r, i) => (
                        <div key={i} className="row gap8" style={{ alignItems: 'flex-start' }}>
                          <input className="input" style={{ width: 130 }} placeholder="维度名"
                            value={r.name}
                            onChange={(e) => setRubric(rubric.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                          <input className="input" style={{ flex: 1 }} placeholder="打分标准描述"
                            value={r.description}
                            onChange={(e) => setRubric(rubric.map((x, j) => (j === i ? { ...x, description: e.target.value } : x)))} />
                          <input className="input mono" style={{ width: 72 }} placeholder="权重" inputMode="decimal"
                            value={r.weight}
                            onChange={(e) => setRubric(rubric.map((x, j) => (j === i ? { ...x, weight: e.target.value } : x)))} />
                          <button className="icon-btn" style={{ height: 38 }} title="删除维度"
                            onClick={() => setRubric(rubric.filter((_, j) => j !== i))}>
                            <Icon name="trash" size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                    <button className="btn btn-soft sm" style={{ marginTop: 12 }}
                      onClick={() => setRubric([...rubric, { name: '', description: '', weight: '1.0' }])}>
                      <Icon name="plus" size={13} />
                      添加维度
                    </button>
                  </>
                )}

                {s.key === 'anchors' && (
                  <>
                    <div className="field-hint" style={{ marginBottom: 10 }}>
                      定义方向「品味」的代表作（可留空）
                    </div>
                    <div className="col gap12">
                      {anchors.map((a, i) => (
                        <div key={i} className="card" style={{ padding: '12px 14px', background: 'var(--surface-2)' }}>
                          <div className="row gap8" style={{ marginBottom: 8 }}>
                            <input className="input" style={{ flex: 1 }} placeholder="论文标题 title（必填）"
                              value={a.title}
                              onChange={(e) => setAnchors(anchors.map((x, j) => (j === i ? { ...x, title: e.target.value } : x)))} />
                            <button className="icon-btn" style={{ height: 38 }} title="删除"
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
                          <input className="input" style={{ width: '100%' }} placeholder="为什么它是这个方向的锚点 reason"
                            value={a.reason ?? ''}
                            onChange={(e) => setAnchors(anchors.map((x, j) => (j === i ? { ...x, reason: e.target.value } : x)))} />
                        </div>
                      ))}
                    </div>
                    <button className="btn btn-soft sm" style={{ marginTop: 12 }}
                      onClick={() => setAnchors([...anchors, { title: '', arxiv_id: '', url: '', reason: '' }])}>
                      <Icon name="plus" size={13} />
                      添加论文
                    </button>
                  </>
                )}

                {s.key === 'synonyms' && (
                  <>
                    <div className="field-hint" style={{ marginBottom: 10 }}>
                      术语 → 同义词（逗号分隔），提升关键词召回
                    </div>
                    <div className="col gap8">
                      {synonyms.map((sy, i) => (
                        <div key={i} className="row gap8">
                          <input className="input" style={{ width: 180 }} placeholder="术语"
                            value={sy.term}
                            onChange={(e) => setSynonyms(synonyms.map((x, j) => (j === i ? { ...x, term: e.target.value } : x)))} />
                          <span style={{ color: 'var(--text-4)' }}>→</span>
                          <input className="input" style={{ flex: 1 }} placeholder="同义词1, 同义词2, …"
                            value={sy.syns}
                            onChange={(e) => setSynonyms(synonyms.map((x, j) => (j === i ? { ...x, syns: e.target.value } : x)))} />
                          <button className="icon-btn" style={{ height: 38 }} title="删除"
                            onClick={() => setSynonyms(synonyms.filter((_, j) => j !== i))}>
                            <Icon name="trash" size={14} />
                          </button>
                        </div>
                      ))}
                      <button className="btn btn-soft sm" style={{ alignSelf: 'flex-start' }}
                        onClick={() => setSynonyms([...synonyms, { term: '', syns: '' }])}>
                        <Icon name="plus" size={13} />
                        添加映射
                      </button>
                    </div>
                  </>
                )}

                {s.key === 'cadence' && (
                  <FormField label="运行节奏" en="Cadence" hint="文献追踪等自动任务的运行频率">
                    <div>
                      <Segmented options={CADENCES.map((c) => ({ v: c.v, label: c.label }))}
                        value={cadence as (typeof CADENCES)[number]['v']}
                        onChange={(v) => setCadence(v)} />
                    </div>
                  </FormField>
                )}
              </AccordionSection>
            ))}
          </div>

          <div className="row gap10" style={{ marginTop: 18 }}>
            <button className="btn btn-ghost" onClick={() => { setFormError(null); setStep(0); }} disabled={busy}>
              返回上一步
            </button>
            <div style={{ flex: 1 }} />
            <button className="btn btn-primary" onClick={() => void create()} disabled={busy}>
              <Icon name="check" size={14} />
              {submitting ? '创建中…' : '创建方向'}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
