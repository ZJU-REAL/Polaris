import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { api, type AnchorPaper, type KeywordSpec, type RubricDimension } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   收录设置共享表单（受控）——建库弹窗与文献库收录设置卡共用。
   五块：文献来源 / arXiv 分类 chips / 检索关键词 chips / 锚点论文 / 打分标准（rubric）；
   顺序即提问顺序——先问「从哪里找」，arXiv 分类只在选了 arXiv 时才出现，
   做临床、做结构的人不该被一个跟自己无关的分类体系拦在第一步；
   ============================================================ */

const QUICK_CATEGORIES = ['cs.CL', 'cs.AI', 'cs.LG', 'cs.CV', 'cs.MA', 'stat.ML'];

// arXiv id 宽松校验：2401.01234 / 2401.01234v2 / 老式 hep-th/9901001
export const ARXIV_ID_RE = /^(\d{4}\.\d{4,5}(v\d+)?|[a-z-]+\/\d{7}(v\d+)?)$/i;

/**
 * 收录设置 → 后端 keywords。
 *
 * 抽成纯函数不是为了复用（只有两处调用），是为了能被断言：选择器点得动、
 * 值却进不了 payload，是一类只看界面发现不了的缺陷——表单看上去完全正常。
 */
export function keywordsFromInclusion(v: InclusionValue): KeywordSpec {
  return {
    sources: v.sources ?? [],
    arxiv_categories: v.arxiv_categories,
    include: v.include,
  };
}

/** 收录设置是否有内容需要下发。只挑了来源也算——它决定去哪儿抓。 */
export function hasInclusionKeywords(v: InclusionValue): boolean {
  return (v.sources ?? []).length > 0 || v.arxiv_categories.length > 0 || v.include.length > 0;
}

export interface InclusionValue {
  /**
   * 这个库从哪些源取文献。空 = 只用 arXiv（存量库行为不变）。
   *
   * 排在 arxiv_categories 之前不是排版偏好：先问「从哪里找」才问得出「怎么筛」。
   * 反过来的话，一个做结构、做临床的人打开建库页，第一个必答题是他领域里
   * 根本不存在的 arXiv 分类。
   */
  sources: string[];
  arxiv_categories: string[];
  include: string[];
  /** 排除关键词：命中即挡在门外。与 include 不同，它在每日同步时也硬过滤。 */
  exclude: string[];
  rubric: RubricDimension[];
  anchors: AnchorPaper[];
}

export interface InclusionSettingsFormProps {
  value: InclusionValue;
  onChange: (v: InclusionValue) => void;
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
  showRubric,
  showAnchors,
  readOnly,
}: InclusionSettingsFormProps) {
  const { arxiv_categories, include, rubric, anchors } = value;
  const sources = value.sources ?? [];
  const exclude = value.exclude ?? [];
  const patch = (p: Partial<InclusionValue>) => onChange({ ...value, ...p });

  // 可选来源问后端要，不在前端写死：装一个源就该立刻可选，撤一个就该立刻消失
  const sourcesQuery = useQuery({
    queryKey: ['literature-sources'],
    queryFn: () => api.listLiteratureSources(),
    retry: false,
  });

  const [customCat, setCustomCat] = useState('');
  const [kwDraft, setKwDraft] = useState('');
  const [exDraft, setExDraft] = useState('');

  function toggleSource(id: string) {
    // 取消最后一个来源＝这个库无处取文献。与其存一个永远抓不到东西的配置，
    // 不如不让它变成空——空值在后端等于「只用 arXiv」，行为可预期
    const next = effectiveSources.includes(id)
      ? effectiveSources.filter((x) => x !== id)
      : [...effectiveSources, id];
    patch({ sources: next.length > 0 ? next : ['arxiv'] });
  }

  function toggleCat(c: string) {
    patch({ arxiv_categories: arxiv_categories.includes(c) ? arxiv_categories.filter((x) => x !== c) : [...arxiv_categories, c] });
  }
  function addCustomCat() {
    const c = customCat.trim();
    if (c && !arxiv_categories.includes(c)) patch({ arxiv_categories: [...arxiv_categories, c] });
    setCustomCat('');
  }

  // —— 关键词 chips（包括 / 排除共用一套增删） ——
  function addKeywords(field: 'include' | 'exclude', raw: string) {
    const parts = raw.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
    if (parts.length === 0) return;
    const current = field === 'include' ? include : exclude;
    const seen = new Set(current.map((x) => x.toLowerCase()));
    const next = [...current];
    for (const p of parts) {
      if (!seen.has(p.toLowerCase())) { next.push(p); seen.add(p.toLowerCase()); }
    }
    patch({ [field]: next } as Partial<InclusionValue>);
    if (field === 'include') setKwDraft(''); else setExDraft('');
  }
  function removeKeyword(field: 'include' | 'exclude', k: string) {
    const current = field === 'include' ? include : exclude;
    patch({ [field]: current.filter((x) => x !== k) } as Partial<InclusionValue>);
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

  // 没配来源 = 只用 arXiv（与后端 DEFAULT_LIBRARY_SOURCES 同口径）
  const effectiveSources = sources.length > 0 ? sources : ['arxiv'];
  // 清单还没到就先显示 id：一个 "pubmed" 也比一块空白说得清楚
  const sourceTitle = (id: string) =>
    (sourcesQuery.data ?? []).find((s) => s.id === id)?.title ?? id;
  const arxivSelected = effectiveSources.includes('arxiv');

  return (
    <div className="col gap16">
      {/* —— 文献来源：先问「从哪里找」 —— */}
      <div className="col gap6">
        <BlockLabel zh="文献来源" en="Literature sources" />
        {readOnly ? (
          /* 只读时摆出十个灰按钮、其中两个亮着，读的人要自己找亮的那几个。
             与下面几块一致：只显示选中的 */
          <div className="row gap6 wrap">
            {effectiveSources.map((id) => (
              <span key={id} className="chip on" style={{ cursor: 'default' }}>
                {sourceTitle(id)}
              </span>
            ))}
          </div>
        ) : sourcesQuery.isError ? (
          <div className="muted" style={{ fontSize: 12.5 }}>
            {tr('无法加载来源列表，将使用 arXiv。', 'Could not load the source list — arXiv will be used.')}
          </div>
        ) : (
          <>
            <div className="row gap6 wrap">
              {(sourcesQuery.data ?? []).map((src) => (
                <button
                  key={src.id}
                  type="button"
                  className={'chip' + (effectiveSources.includes(src.id) ? ' on' : '')}
                  title={src.description}
                  onClick={() => toggleSource(src.id)}
                >
                  {src.title}
                </button>
              ))}
            </div>
            <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
              {tr(
                '按你的领域挑：做生物选 PubMed / Europe PMC，做化学与工程选 Crossref，做 CS / 物理选 arXiv。不选则只用 arXiv。',
                'Pick what your field uses: PubMed / Europe PMC for life sciences, Crossref for chemistry and engineering, arXiv for CS and physics. Defaults to arXiv.',
              )}
            </div>
          </>
        )}
      </div>

      {/* —— arXiv 分类：只在选了 arXiv 时出现 —— */}
      {arxivSelected && (
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
            <div className="muted" style={{ fontSize: 12.5 }}>
              {/* 曾写「使用默认分类」——而默认回退早在 #720 A4 就删掉了，
                  实际是完全不带分类过滤。照抄旧文案等于告诉用户一件没发生的事 */}
              {tr('未限定分类，按关键词检索全站', 'No category filter — searching by keywords')}
            </div>
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
      )}

      {/* —— 检索关键词 chips —— */}
      <div className="col gap6">
        <BlockLabel zh="包括关键词" en="Include terms" />
        {include.length > 0 ? (
          <div className="row gap6 wrap">
            {include.map((k) => (
              <span key={k} className="chip on" style={{ gap: 4, cursor: 'default' }}>
                {k}
                {!readOnly && (
                  <button
                    type="button"
                    aria-label={tr('删除', 'Remove')}
                    onClick={() => removeKeyword('include', k)}
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
              if (/[,，]/.test(v)) addKeywords('include', v);
              else setKwDraft(v);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); addKeywords('include', kwDraft); }
              else if (e.key === 'Backspace' && !kwDraft && include.length > 0) {
                e.preventDefault();
                removeKeyword('include', include[include.length - 1]!);
              }
            }}
            onBlur={() => { if (kwDraft.trim()) addKeywords('include', kwDraft); }}
            placeholder={tr('输入关键词后回车或逗号添加，如 agent', 'Type a term, press Enter or comma, e.g. agent')}
          />
        )}
      </div>

      {/* —— 排除关键词 chips —— */}
      <div className="col gap6">
        <BlockLabel zh="排除关键词" en="Exclude terms" />
        <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
          {tr(
            '命中就不收，检索、打分、每日同步三处都生效。用来挡掉和本方向撞词的其他领域，宁缺毋滥。',
            'Matching papers are never admitted — in search, in scoring, and in the daily sync. Use it for other fields that share vocabulary with yours; keep it short.',
          )}
        </div>
        {exclude.length > 0 ? (
          <div className="row gap6 wrap">
            {exclude.map((k) => (
              <span key={k} className="chip" style={{ gap: 4, cursor: 'default' }}>
                {k}
                {!readOnly && (
                  <button
                    type="button"
                    aria-label={tr('删除', 'Remove')}
                    onClick={() => removeKeyword('exclude', k)}
                    style={{ display: 'inline-flex', background: 'none', border: 'none', padding: 0, marginLeft: 2, cursor: 'pointer', color: 'inherit' }}
                  >
                    <Icon name="x" size={11} />
                  </button>
                )}
              </span>
            ))}
          </div>
        ) : readOnly ? (
          <div className="muted" style={{ fontSize: 12.5 }}>{tr('未设排除关键词', 'No exclude terms set')}</div>
        ) : null}
        {!readOnly && (
          <input
            className="input"
            value={exDraft}
            onChange={(e) => {
              const v = e.target.value;
              if (/[,，]/.test(v)) addKeywords('exclude', v);
              else setExDraft(v);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); addKeywords('exclude', exDraft); }
              else if (e.key === 'Backspace' && !exDraft && exclude.length > 0) {
                e.preventDefault();
                removeKeyword('exclude', exclude[exclude.length - 1]!);
              }
            }}
            onBlur={() => { if (exDraft.trim()) addKeywords('exclude', exDraft); }}
            placeholder={tr('输入后回车或逗号添加，如 speech recognition', 'Type a term, press Enter or comma')}
          />
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
