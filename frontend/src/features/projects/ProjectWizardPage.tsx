import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { FormField } from '../../components/ui/FormField';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { api, type AnchorPaper, type ProjectDefinition, type RubricDimension } from '../../lib/api';

/* ============================================================
   /projects/new — 多步访谈向导（7 步），逐步构建 definition。
   任意步可「存草稿」（首次 POST，之后 PATCH 同一草稿）。
   ============================================================ */

const STEPS = [
  { zh: '方向定义', en: 'Statement' },
  { zh: '目标与范围', en: 'Goals & Scope' },
  { zh: '研究问题', en: 'Questions' },
  { zh: '打分 Rubric', en: 'Rubric' },
  { zh: '锚点论文', en: 'Anchors' },
  { zh: '关键词', en: 'Keywords' },
  { zh: '节奏与确认', en: 'Confirm' },
] as const;

const COMMON_CATEGORIES = [
  'cs.CL', 'cs.LG', 'cs.AI', 'cs.CV', 'cs.IR', 'cs.NE', 'cs.RO',
  'cs.SE', 'cs.CR', 'cs.DC', 'cs.HC', 'cs.MA', 'stat.ML', 'eess.AS', 'eess.IV',
];

const CADENCES = [
  { v: 'daily', label: '每日 daily' },
  { v: 'weekly', label: '每周 weekly' },
  { v: 'manual', label: '手动 manual' },
] as const;

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

export function ProjectWizardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setCurrentProjectId } = useProject();

  const [step, setStep] = useState(0);
  const [stepError, setStepError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [draftId, setDraftId] = useState<string | null>(null);

  // —— 各步状态 ——
  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  const [goals, setGoals] = useState<string[]>([]);
  const [inScope, setInScope] = useState<string[]>([]);
  const [outScope, setOutScope] = useState<string[]>([]);
  const [questions, setQuestions] = useState<string[]>([]);
  const [rubric, setRubric] = useState<RubricRow[]>([
    { name: '新颖性', description: '与已有工作的差异化程度', weight: '1.0' },
    { name: '可行性', description: '现有资源下能否完成实验验证', weight: '1.0' },
  ]);
  const [anchors, setAnchors] = useState<AnchorPaper[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [customCat, setCustomCat] = useState('');
  const [includeTerms, setIncludeTerms] = useState<string[]>([]);
  const [synonyms, setSynonyms] = useState<SynonymRow[]>([]);
  const [cadence, setCadence] = useState<string>('daily');

  function buildDefinition(): ProjectDefinition {
    const def: ProjectDefinition = {};
    if (statement.trim()) def.statement = statement.trim();
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
    if (categories.length || includeTerms.length || Object.keys(syn).length) {
      def.keywords = {
        ...(categories.length ? { arxiv_categories: categories } : {}),
        ...(includeTerms.length ? { include: includeTerms } : {}),
        ...(Object.keys(syn).length ? { synonyms: syn } : {}),
      };
    }
    def.cadence = cadence;
    return def;
  }

  function validateStep(s: number): string | null {
    switch (s) {
      case 0:
        if (!name.trim()) return '请填写方向名称';
        if (!statement.trim()) return '请用一句话定义这个研究方向';
        return null;
      case 1:
        if (goals.length === 0) return '至少填写一个目标';
        return null;
      case 2:
        if (questions.length === 0) return '至少填写一个研究问题（建议 3–5 个）';
        return null;
      case 3: {
        const valid = rubric.filter((r) => r.name.trim());
        if (valid.length === 0) return '至少保留一个打分维度';
        for (const r of valid) {
          const w = Number(r.weight);
          if (!Number.isFinite(w) || w <= 0) return `维度「${r.name}」的权重必须是正数`;
        }
        return null;
      }
      case 4:
        for (const a of anchors) {
          if (!a.title.trim() && (a.arxiv_id?.trim() || a.url?.trim() || a.reason?.trim()))
            return '锚点论文的 title 不能为空';
        }
        return null;
      case 5:
        if (categories.length === 0 && includeTerms.length === 0)
          return '至少选择一个 arXiv 分类或添加一个 include 关键词';
        return null;
      default:
        return null;
    }
  }

  function next() {
    const err = validateStep(step);
    setStepError(err);
    if (err) return;
    setStep((s) => Math.min(s + 1, STEPS.length - 1));
  }
  function prev() {
    setStepError(null);
    setStep((s) => Math.max(s - 1, 0));
  }

  async function persist(finalize: boolean): Promise<void> {
    setSaving(true);
    try {
      const payload = { name: name.trim() || '未命名方向', definition: buildDefinition() };
      let id = draftId;
      if (id) {
        await api.patchProject(id, payload);
      } else {
        const created = await api.createProject(payload);
        id = created.id;
        setDraftId(id);
      }
      await queryClient.invalidateQueries({ queryKey: ['projects'] });
      if (finalize && id) {
        setCurrentProjectId(id);
        toast('研究方向已创建', 'ok');
        navigate(`/projects/${id}`);
      } else {
        toast('草稿已保存', 'ok');
      }
    } catch (err) {
      toast(`保存失败：${err instanceof Error ? err.message : String(err)}`, 'error');
    } finally {
      setSaving(false);
    }
  }

  function submit() {
    for (let s = 0; s < STEPS.length; s++) {
      const err = validateStep(s);
      if (err) {
        setStep(s);
        setStepError(err);
        return;
      }
    }
    void persist(true);
  }

  function toggleCategory(c: string) {
    setCategories((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]));
  }

  const def = buildDefinition();

  return (
    <div className="page fadeup" style={{ maxWidth: 860 }}>
      <PageHead
        eyebrow="Polaris · Directions"
        title="新建研究方向"
        sub="通过一次结构化访谈，把模糊的兴趣固化为可执行的方向定义。"
        en="Direction interview wizard"
      />

      {/* 步骤指示器 */}
      <div className="wiz-steps">
        {STEPS.map((s, i) => (
          <div
            key={s.en}
            className={'wiz-step' + (i === step ? ' on' : i < step ? ' done' : '')}
            onClick={() => {
              if (i < step) {
                setStepError(null);
                setStep(i);
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

      <div className="card card-pad" style={{ minHeight: 320 }}>
        {step === 0 && (
          <>
            <FormField label="方向名称" en="Name" hint="将显示在侧栏与项目列表中">
              <input className="input" value={name} onChange={(e) => setName(e.target.value)}
                placeholder="如：LLM 自主科研智能体" />
            </FormField>
            <FormField label="一句话定义" en="Statement" hint="用一句话说清这个方向研究什么、为什么重要">
              <textarea className="textarea" rows={3} value={statement} onChange={(e) => setStatement(e.target.value)}
                placeholder="如：研究让 LLM agent 端到端完成文献调研 → idea → 实验 → 论文的方法与系统" />
            </FormField>
          </>
        )}

        {step === 1 && (
          <>
            <FormField label="研究目标" en="Goals" hint="这个方向希望达成什么（至少 1 条）">
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

        {step === 2 && (
          <FormField label="具体研究问题" en="Research questions" hint="建议 3–5 个可验证的具体问题">
            <ListEditor items={questions} onChange={setQuestions} placeholder="如：citation-graph 重排序能否降低新颖性幻觉？" />
          </FormField>
        )}

        {step === 3 && (
          <>
            <div className="field-label" style={{ marginBottom: 10 }}>
              打分维度<span className="en">Rubric — 论文相关性/idea 质量打分标准，可增删维度与权重</span>
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

        {step === 4 && (
          <>
            <div className="field-label" style={{ marginBottom: 10 }}>
              锚点论文<span className="en">Anchor papers — 定义方向「品味」的代表作（可留空）</span>
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

        {step === 5 && (
          <>
            <FormField label="arXiv 分类" en="Categories" hint="点击切换；也可输入自定义分类">
              <div className="row gap6 wrap">
                {COMMON_CATEGORIES.map((c) => (
                  <button key={c} type="button" className={'chip mono' + (categories.includes(c) ? ' on' : '')}
                    onClick={() => toggleCategory(c)}>
                    {c}
                  </button>
                ))}
                {categories.filter((c) => !COMMON_CATEGORIES.includes(c)).map((c) => (
                  <button key={c} type="button" className="chip mono on" onClick={() => toggleCategory(c)} title="点击移除">
                    {c} ×
                  </button>
                ))}
              </div>
              <div className="row gap8" style={{ marginTop: 8 }}>
                <input className="input mono" style={{ width: 200 }} placeholder="自定义，如 q-bio.NC"
                  value={customCat}
                  onChange={(e) => setCustomCat(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      const v = customCat.trim();
                      if (v && !categories.includes(v)) setCategories([...categories, v]);
                      setCustomCat('');
                    }
                  }} />
                <button className="btn btn-soft sm" style={{ height: 38 }}
                  onClick={() => {
                    const v = customCat.trim();
                    if (v && !categories.includes(v)) setCategories([...categories, v]);
                    setCustomCat('');
                  }}>
                  <Icon name="plus" size={13} />
                  添加
                </button>
              </div>
            </FormField>
            <FormField label="Include 关键词" en="Include terms" hint="标题/摘要命中这些词才进入相关性判定">
              <ListEditor items={includeTerms} onChange={setIncludeTerms} placeholder="如 agent、tool use" />
            </FormField>
            <div className="field-label" style={{ marginBottom: 8 }}>
              同义词映射<span className="en">Synonyms — 术语 → 同义词（逗号分隔）</span>
            </div>
            <div className="col gap8">
              {synonyms.map((s, i) => (
                <div key={i} className="row gap8">
                  <input className="input" style={{ width: 180 }} placeholder="术语"
                    value={s.term}
                    onChange={(e) => setSynonyms(synonyms.map((x, j) => (j === i ? { ...x, term: e.target.value } : x)))} />
                  <span style={{ color: 'var(--text-4)' }}>→</span>
                  <input className="input" style={{ flex: 1 }} placeholder="同义词1, 同义词2, …"
                    value={s.syns}
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

        {step === 6 && (
          <>
            <FormField label="运行节奏" en="Cadence" hint="文献追踪等自动任务的运行频率">
              <div>
                <Segmented options={CADENCES.map((c) => ({ v: c.v, label: c.label }))} value={cadence as (typeof CADENCES)[number]['v']}
                  onChange={(v) => setCadence(v)} />
              </div>
            </FormField>
            <div className="field-label" style={{ margin: '6px 0 8px' }}>
              定义预览<span className="en">definition JSON — 提交前请确认</span>
            </div>
            <div className="codeblock scroll" style={{ maxHeight: 320, overflowY: 'auto' }}>
              {JSON.stringify({ name: name.trim() || '未命名方向', definition: def }, null, 2)}
            </div>
          </>
        )}

        {stepError && <div className="field-error" style={{ marginTop: 14 }}>{stepError}</div>}
      </div>

      {/* 底部操作 */}
      <div className="row gap10" style={{ marginTop: 18 }}>
        <button className="btn btn-ghost" onClick={prev} disabled={step === 0 || saving}>
          上一步
        </button>
        <button className="btn btn-soft" onClick={() => void persist(false)} disabled={saving}>
          {draftId ? '更新草稿' : '存草稿'}
        </button>
        <div style={{ flex: 1 }} />
        {step < STEPS.length - 1 ? (
          <button className="btn btn-primary" onClick={next} disabled={saving}>
            下一步
            <Icon name="arrow" size={14} />
          </button>
        ) : (
          <button className="btn btn-primary" onClick={submit} disabled={saving}>
            <Icon name="check" size={14} />
            {saving ? '提交中…' : '创建方向'}
          </button>
        )}
      </div>
    </div>
  );
}
