import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type AnchorPaper, type RubricDimension } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   收录设置共享表单（受控）——建库弹窗与文献库「收录设置」卡共用。
   四块：arXiv 分类 chips / 检索关键词 chips / 锚点论文 / 打分标准（rubric）；
   顶部「AI 自动生成」按名称+说明推荐一整套设置填进表单，供用户改后保存。
   ============================================================ */

const QUICK_CATEGORIES = ['cs.CL', 'cs.AI', 'cs.LG', 'cs.CV', 'cs.MA', 'stat.ML'];

// arXiv id 宽松校验：2401.01234 / 2401.01234v2 / 老式 hep-th/9901001
export const ARXIV_ID_RE = /^(\d{4}\.\d{4,5}(v\d+)?|[a-z-]+\/\d{7}(v\d+)?)$/i;

export interface InclusionValue {
  arxiv_categories: string[];
  include: string[];
  rubric: RubricDimension[];
  anchors: AnchorPaper[];
}

export interface InclusionSettingsFormProps {
  value: InclusionValue;
  onChange: (v: InclusionValue) => void;
  /** 用于 AI 自动生成（名称/说明为空时按钮禁用） */
  name: string;
  statement: string;
  showRubric?: boolean;
  showAnchors?: boolean;
  /** 只读：隐藏 AI 生成 / 增删按钮，禁用所有输入，仅展示已配置项。 */
  readOnly?: boolean;
}

function BlockLabel({ zh, en, right }: { zh: string; en: string; right?: React.ReactNode }) {
  return (
    <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
      <span className="muted" style={{ fontSize: 12, fontWeight: 600 }}>{tr(zh, en)}</span>
      {right}
    </div>
  );
}

export function InclusionSettingsForm({
  value,
  onChange,
  name,
  statement,
  showRubric,
  showAnchors,
  readOnly,
}: InclusionSettingsFormProps) {
  const { arxiv_categories, include, rubric, anchors } = value;
  const patch = (p: Partial<InclusionValue>) => onChange({ ...value, ...p });

  const [customCat, setCustomCat] = useState('');
  const [kwDraft, setKwDraft] = useState('');

  // —— AI 自动生成 ——
  const canSuggest = name.trim().length > 0 && statement.trim().length > 0;
  const suggest = useMutation({
    mutationFn: () => api.suggestLibraryDefinition({ name: name.trim(), statement: statement.trim() }),
    onSuccess: (d) => {
      // 非破坏性合并：并集分类/关键词，追加不重名的 rubric 维度与不重复的锚点。
      const cats = [...new Set([...arxiv_categories, ...(d.keywords.arxiv_categories ?? [])])];
      const haveInc = new Set(include.map((x) => x.toLowerCase()));
      const inc = [...include, ...(d.keywords.include ?? []).filter((x) => x.trim() && !haveInc.has(x.trim().toLowerCase()))];
      const haveRub = new Set(rubric.map((r) => r.name.trim().toLowerCase()));
      const rub = [...rubric, ...(d.rubric ?? []).filter((r) => r.name.trim() && !haveRub.has(r.name.trim().toLowerCase()))];
      const haveAnc = new Set(anchors.map((a) => (a.arxiv_id || a.title || '').trim().toLowerCase()));
      const anc = [...anchors, ...(d.anchors ?? []).filter((a) => {
        const key = (a.arxiv_id || a.title || '').trim().toLowerCase();
        return key.length > 0 && !haveAnc.has(key);
      })];
      onChange({
        arxiv_categories: cats,
        include: inc,
        rubric: showRubric ? rub : rubric,
        anchors: showAnchors ? anc : anchors,
      });
      toast(tr('已用 AI 生成，请检查后保存', 'AI-generated — review and save'), 'ok');
    },
    onError: (e) => toast(`${tr('生成失败：', 'Generate failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // —— arXiv 分类 ——
  function toggleCat(c: string) {
    patch({ arxiv_categories: arxiv_categories.includes(c) ? arxiv_categories.filter((x) => x !== c) : [...arxiv_categories, c] });
  }
  function addCustomCat() {
    const c = customCat.trim();
    if (c && !arxiv_categories.includes(c)) patch({ arxiv_categories: [...arxiv_categories, c] });
    setCustomCat('');
  }

  // —— 检索关键词 chips ——
  function addKeywords(raw: string) {
    const parts = raw.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
    if (parts.length === 0) return;
    const seen = new Set(include.map((x) => x.toLowerCase()));
    const next = [...include];
    for (const p of parts) {
      if (!seen.has(p.toLowerCase())) { next.push(p); seen.add(p.toLowerCase()); }
    }
    patch({ include: next });
    setKwDraft('');
  }
  function removeKeyword(k: string) {
    patch({ include: include.filter((x) => x !== k) });
  }

  // —— 锚点论文 ——
  function updateAnchor(i: number, p: Partial<AnchorPaper>) {
    patch({ anchors: anchors.map((a, j) => (j === i ? { ...a, ...p } : a)) });
  }
  function addAnchor() {
    patch({ anchors: [...anchors, { title: '' }] });
  }
  function removeAnchor(i: number) {
    patch({ anchors: anchors.filter((_, j) => j !== i) });
  }

  // —— 打分标准（rubric） ——
  function updateRubric(i: number, p: Partial<RubricDimension>) {
    patch({ rubric: rubric.map((r, j) => (j === i ? { ...r, ...p } : r)) });
  }
  function addRubric() {
    patch({ rubric: [...rubric, { name: '', description: '', weight: 1 }] });
  }
  function removeRubric(i: number) {
    patch({ rubric: rubric.filter((_, j) => j !== i) });
  }

  return (
    <div className="col gap16">
      {/* —— AI 自动生成 —— */}
      {!readOnly && (
      <div className="row gap10" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <button
          type="button"
          className="btn btn-soft sm"
          disabled={!canSuggest || suggest.isPending}
          onClick={() => suggest.mutate()}
        >
          {suggest.isPending ? (
            <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
          ) : (
            <Icon name="sparkle" size={13} />
          )}
          {suggest.isPending ? tr('生成中…', 'Generating…') : tr('AI 自动生成', 'Auto-generate with AI')}
        </button>
        <span className="muted" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
          {canSuggest
            ? tr('根据名称与说明推荐分类、关键词、锚点与打分标准', 'Suggests categories, keywords, anchors and rubric from the name & statement')
            : tr('先填名称和描述', 'Fill in the name and statement first')}
        </span>
      </div>
      )}

      {/* —— arXiv 分类 —— */}
      <div className="col gap6">
        <BlockLabel zh="arXiv 分类" en="arXiv categories" />
        {readOnly ? (
          arxiv_categories.length > 0 ? (
            <div className="row gap6 wrap">
              {arxiv_categories.map((c) => (
                <span key={c} className="chip mono on" style={{ cursor: 'default' }}>{c}</span>
              ))}
            </div>
          ) : (
            <div className="muted" style={{ fontSize: 12.5 }}>{tr('使用默认分类', 'Using default categories')}</div>
          )
        ) : (
          <>
            <div className="row gap6 wrap">
              {[...new Set([...QUICK_CATEGORIES, ...arxiv_categories])].map((c) => (
                <button
                  key={c}
                  type="button"
                  className={'chip mono' + (arxiv_categories.includes(c) ? ' on' : '')}
                  onClick={() => toggleCat(c)}
                >
                  {c}
                </button>
              ))}
            </div>
            <div className="row gap8" style={{ marginTop: 2 }}>
              <input
                className="input"
                style={{ width: 170 }}
                placeholder={tr('自定义分类，如 cs.IR', 'custom, e.g. cs.IR')}
                value={customCat}
                onChange={(e) => setCustomCat(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addCustomCat(); } }}
              />
              <button type="button" className="btn btn-soft sm" onClick={addCustomCat} disabled={!customCat.trim()}>
                {tr('添加', 'Add')}
              </button>
            </div>
          </>
        )}
      </div>

      {/* —— 检索关键词 chips —— */}
      <div className="col gap6">
        <BlockLabel zh="检索关键词" en="Search terms" />
        {include.length > 0 ? (
          <div className="row gap6 wrap">
            {include.map((k) => (
              <span key={k} className="chip on" style={{ gap: 4, cursor: 'default' }}>
                {k}
                {!readOnly && (
                  <button
                    type="button"
                    aria-label={tr('删除', 'Remove')}
                    onClick={() => removeKeyword(k)}
                    style={{ display: 'inline-flex', background: 'none', border: 'none', padding: 0, marginLeft: 2, cursor: 'pointer', color: 'inherit' }}
                  >
                    <Icon name="x" size={11} />
                  </button>
                )}
              </span>
            ))}
          </div>
        ) : readOnly ? (
          <div className="muted" style={{ fontSize: 12.5 }}>{tr('未设检索关键词', 'No search terms set')}</div>
        ) : null}
        {!readOnly && (
          <input
            className="input"
            value={kwDraft}
            onChange={(e) => {
              const v = e.target.value;
              // 输入逗号即时成词
              if (/[,，]/.test(v)) addKeywords(v);
              else setKwDraft(v);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); addKeywords(kwDraft); }
              else if (e.key === 'Backspace' && !kwDraft && include.length > 0) {
                e.preventDefault();
                removeKeyword(include[include.length - 1]!);
              }
            }}
            onBlur={() => { if (kwDraft.trim()) addKeywords(kwDraft); }}
            placeholder={tr('输入关键词后回车或逗号添加，如 agent', 'Type a term, press Enter or comma, e.g. agent')}
          />
        )}
        {!readOnly && (
          <div className="field-hint">
            {tr('文献追踪按这些关键词与上面的分类检索候选论文；留空则用默认分类。', 'Literature tracking searches candidates by these terms plus the categories above; leave empty for defaults.')}
          </div>
        )}
      </div>

      {/* —— 锚点论文 —— */}
      {showAnchors && (
        <div className="col gap8">
          <BlockLabel
            zh="锚点论文"
            en="Anchor papers"
            right={
              readOnly ? undefined : (
                <button type="button" className="btn btn-soft sm" onClick={addAnchor}>
                  <Icon name="plus" size={12} />
                  {tr('添加论文', 'Add paper')}
                </button>
              )
            }
          />
          {anchors.length === 0 ? (
            <div className="muted" style={{ fontSize: 12.5 }}>
              {readOnly
                ? tr('未设锚点论文。', 'No anchor papers set.')
                : tr('可留空；抓取时会解析这些论文并做参考文献扩展。', 'Optional; ingest resolves these and expands references.')}
            </div>
          ) : (
            <div className="col gap10">
              {anchors.map((a, i) => {
                const badId = !!a.arxiv_id && a.arxiv_id.trim() !== '' && !ARXIV_ID_RE.test(a.arxiv_id.trim());
                return (
                  <div key={i} style={{ border: '0.5px solid var(--border)', borderRadius: 9, padding: 12 }}>
                    <div className="row gap8" style={{ alignItems: 'flex-start' }}>
                      <div className="col gap8" style={{ flex: 1, minWidth: 0 }}>
                        <label className="col gap4">
                          <span className="muted" style={{ fontSize: 11.5 }}>{tr('标题（必填）', 'Title (required)')}</span>
                          <input
                            className="input"
                            value={a.title}
                            disabled={readOnly}
                            onChange={(e) => updateAnchor(i, { title: e.target.value })}
                            placeholder={tr('论文标题', 'Paper title')}
                          />
                        </label>
                        <div className="row gap8 wrap">
                          <label className="col gap4" style={{ width: 220 }}>
                            <span className="muted" style={{ fontSize: 11.5 }}>{tr('arXiv 编号（选填）', 'arXiv id (optional)')}</span>
                            <input
                              className="input mono"
                              style={{ fontSize: 12.5 }}
                              value={a.arxiv_id ?? ''}
                              disabled={readOnly}
                              onChange={(e) => updateAnchor(i, { arxiv_id: e.target.value })}
                              placeholder="2401.01234"
                            />
                          </label>
                          <label className="col gap4" style={{ flex: 1, minWidth: 180 }}>
                            <span className="muted" style={{ fontSize: 11.5 }}>{tr('入选理由（选填）', 'Reason (optional)')}</span>
                            <input
                              className="input"
                              value={a.reason ?? ''}
                              disabled={readOnly}
                              onChange={(e) => updateAnchor(i, { reason: e.target.value })}
                              placeholder={tr('为什么把它当作锚点', 'Why it anchors this direction')}
                            />
                          </label>
                        </div>
                        {badId && (
                          <div style={{ color: 'var(--danger-tx)', fontSize: 11.5 }}>
                            {tr('不是合法 arXiv 编号', 'Not a valid arXiv id')}
                          </div>
                        )}
                      </div>
                      {!readOnly && (
                        <button
                          type="button"
                          className="icon-btn"
                          title={tr('删除论文', 'Remove paper')}
                          aria-label={tr('删除论文', 'Remove paper')}
                          onClick={() => removeAnchor(i)}
                          style={{ flexShrink: 0 }}
                        >
                          <Icon name="trash" size={14} />
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* —— 打分标准（rubric） —— */}
      {showRubric && (
        <div className="col gap8">
          <BlockLabel
            zh="打分标准"
            en="Scoring rubric"
            right={
              readOnly ? undefined : (
                <button type="button" className="btn btn-soft sm" onClick={addRubric}>
                  <Icon name="plus" size={12} />
                  {tr('添加维度', 'Add dimension')}
                </button>
              )
            }
          />
          {rubric.length === 0 ? (
            <div className="muted" style={{ fontSize: 12.5 }}>
              {tr('未设维度：只按方向说明判定相关性。', 'No dimensions — relevance is judged by the statement only.')}
            </div>
          ) : (
            <div className="col gap10">
              {rubric.map((r, i) => (
                <div key={i} style={{ border: '0.5px solid var(--border)', borderRadius: 9, padding: 12 }}>
                  <div className="row gap8" style={{ alignItems: 'flex-start' }}>
                    <div className="col gap8" style={{ flex: 1, minWidth: 0 }}>
                      <label className="col gap4">
                        <span className="muted" style={{ fontSize: 11.5 }}>{tr('维度名', 'Dimension name')}</span>
                        <input
                          className="input"
                          value={r.name}
                          disabled={readOnly}
                          onChange={(e) => updateRubric(i, { name: e.target.value })}
                          placeholder={tr('如 方法新颖性', 'e.g. Methodological novelty')}
                        />
                      </label>
                      <label className="col gap4">
                        <span className="muted" style={{ fontSize: 11.5 }}>{tr('打分标准描述', 'What counts as a good score')}</span>
                        <textarea
                          className="textarea"
                          rows={2}
                          value={r.description}
                          disabled={readOnly}
                          onChange={(e) => updateRubric(i, { description: e.target.value })}
                          placeholder={tr('这一维度怎样算高分', 'Describe what a high score looks like on this dimension')}
                        />
                      </label>
                      <div className="col gap4">
                        <span className="muted" style={{ fontSize: 11.5 }}>
                          {tr('权重', 'Weight')}
                          <span className="mono" style={{ marginLeft: 8, fontWeight: 650, color: 'var(--text-2)' }}>
                            {r.weight.toFixed(1)}
                          </span>
                        </span>
                        <input
                          type="range"
                          min={0}
                          max={3}
                          step={0.1}
                          value={r.weight}
                          disabled={readOnly}
                          onChange={(e) => updateRubric(i, { weight: Number(e.target.value) })}
                          style={{ width: 220, maxWidth: '100%' }}
                        />
                      </div>
                    </div>
                    {!readOnly && (
                      <button
                        type="button"
                        className="icon-btn"
                        title={tr('删除维度', 'Remove dimension')}
                        aria-label={tr('删除维度', 'Remove dimension')}
                        onClick={() => removeRubric(i)}
                        style={{ flexShrink: 0 }}
                      >
                        <Icon name="trash" size={14} />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
