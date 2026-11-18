import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import type { ResearchBrief } from '../../lib/researchApi';

/* ============================================================
   检索范围编辑：名称、检索词、文献来源、时间范围、论文上限。
   两处用：开始前的「调整范围」，以及检索后想换一组关键词重新检索。
   只管编辑和交出结果；开始 / 重新检索由调用方做。Esc = 取消。
   ============================================================ */

export const DEFAULT_MAX_PAPERS = 200;

export interface BriefEditorProps {
  brief: ResearchBrief;
  /** 主按钮上的字：开始研究 / 重新检索 */
  submitLabel: string;
  busyLabel: string;
  busy: boolean;
  onSubmit: (brief: ResearchBrief) => void;
  onCancel: () => void;
  /** 打开时光标放哪：改名字还是改关键词 */
  focus?: 'name' | 'keywords';
  /** 不显示名称一栏（课题已经有名字、这里只改检索范围时） */
  hideName?: boolean;
}

export function BriefEditor({ brief, submitLabel, busyLabel, busy, onSubmit, onCancel, focus = 'name', hideName }: BriefEditorProps) {
  const [draft, setDraft] = useState<ResearchBrief>(brief);
  const [kw, setKw] = useState('');
  const nameRef = useRef<HTMLInputElement | null>(null);
  const kwRef = useRef<HTMLInputElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);

  // 来源的可选项来自引擎（装了哪些文献来源）；取不到时只列范围里已有的那几个
  const { data: sourceOptions } = useQuery({
    queryKey: ['literature-sources'],
    queryFn: () => api.listLiteratureSources(),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const sources =
    sourceOptions && sourceOptions.length > 0
      ? sourceOptions.map((s) => ({ id: s.id, title: s.title, description: s.description }))
      : brief.sources.map((id) => ({ id, title: id, description: '' }));

  useEffect(() => {
    (focus === 'keywords' || hideName ? kwRef.current : nameRef.current)?.focus();
    rootRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [focus, hideName]);

  const patch = (p: Partial<ResearchBrief>) => setDraft((d) => ({ ...d, ...p }));

  function addKeyword() {
    const parts = kw
      .split(/[,，;；\n]/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (parts.length === 0) return;
    const have = new Set(draft.include_keywords.map((k) => k.toLowerCase()));
    const fresh = parts.filter((k) => !have.has(k.toLowerCase()));
    patch({ include_keywords: [...draft.include_keywords, ...fresh] });
    setKw('');
  }

  const noKeywords = draft.include_keywords.length === 0 && kw.trim() === '';
  const noSources = draft.sources.length === 0;
  const noName = !hideName && draft.title.trim() === '';
  const problem = noKeywords
    ? tr('至少保留一个关键词', 'Keep at least one keyword.')
    : noSources
      ? tr('至少选择一个文献来源', 'Pick at least one source.')
      : noName
        ? tr('名称不能为空', 'Enter a name.')
        : null;

  function submit() {
    if (problem || busy) return;
    // 输入框里还没回车的那个词也算数
    const pending = kw.trim();
    const include =
      pending && !draft.include_keywords.some((k) => k.toLowerCase() === pending.toLowerCase())
        ? [...draft.include_keywords, pending]
        : draft.include_keywords;
    onSubmit({ ...draft, title: draft.title.trim() || brief.title, include_keywords: include });
  }

  const months = String([12, 36, 60].includes(draft.months_back) ? draft.months_back : 36) as '12' | '36' | '60';
  const cap = String(draft.max_papers ?? DEFAULT_MAX_PAPERS) as '100' | '200' | '300';

  return (
    <div
      ref={rootRef}
      className="dr-brief"
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onCancel();
        }
      }}
    >
      {draft.statement && <p className="dr-brief-statement">{draft.statement}</p>}

      {!hideName && (
        <div className="dr-brief-row">
          <label className="dr-brief-label" htmlFor="dr-brief-name">
            {tr('名称', 'Name')}
          </label>
          <input
            id="dr-brief-name"
            ref={nameRef}
            className="input"
            value={draft.title}
            maxLength={120}
            onChange={(e) => patch({ title: e.target.value })}
          />
        </div>
      )}

      <div className="dr-brief-row">
        <label className="dr-brief-label" htmlFor="dr-brief-kw">
          {tr('检索关键词', 'Search keywords')}
        </label>
        <div>
          <div className="dr-chips">
            {draft.include_keywords.map((k) => (
              <span key={k} className="dr-chip">
                {k}
                <button
                  type="button"
                  aria-label={tr(`移除 ${k}`, `Remove ${k}`)}
                  onClick={() => patch({ include_keywords: draft.include_keywords.filter((x) => x !== k) })}
                >
                  <Icon name="x" size={10} />
                </button>
              </span>
            ))}
            <input
              id="dr-brief-kw"
              ref={kwRef}
              className="dr-chip-input"
              value={kw}
              placeholder={tr('添加关键词，回车确认', 'Add a keyword, press Enter')}
              onChange={(e) => setKw(e.target.value)}
              onBlur={addKeyword}
              onKeyDown={(e) => {
                if (e.nativeEvent.isComposing || e.keyCode === 229) return;
                if (e.key === 'Enter' || e.key === ',') {
                  e.preventDefault();
                  addKeyword();
                } else if (e.key === 'Backspace' && kw === '' && draft.include_keywords.length > 0) {
                  patch({ include_keywords: draft.include_keywords.slice(0, -1) });
                }
              }}
            />
          </div>
          <div className="hint" style={{ marginTop: 4 }}>
            {tr('用英文，文献来源按英文检索。', 'In English, as the sources are searched in English.')}
          </div>
        </div>
      </div>

      <div className="dr-brief-row">
        <span className="dr-brief-label">{tr('文献来源', 'Sources')}</span>
        <div className="dr-chips plain">
          {sources.map((s) => {
            const on = draft.sources.includes(s.id);
            return (
              <button
                key={s.id}
                type="button"
                className={'chip' + (on ? ' on' : '')}
                aria-pressed={on}
                title={s.description || undefined}
                onClick={() =>
                  patch({ sources: on ? draft.sources.filter((x) => x !== s.id) : [...draft.sources, s.id] })
                }
              >
                {s.title}
              </button>
            );
          })}
        </div>
      </div>

      <div className="dr-brief-row">
        <span className="dr-brief-label">{tr('时间范围', 'Published within')}</span>
        <div>
          <Segmented
            value={months}
            onChange={(v) => patch({ months_back: Number(v) })}
            options={[
              { v: '12', label: tr('近 1 年', '1 year') },
              { v: '36', label: tr('近 3 年', '3 years') },
              { v: '60', label: tr('近 5 年', '5 years') },
            ]}
          />
        </div>
      </div>

      <div className="dr-brief-row">
        <span className="dr-brief-label">{tr('论文上限', 'Paper limit')}</span>
        <div>
          <Segmented
            value={cap}
            onChange={(v) => patch({ max_papers: Number(v) })}
            options={[
              { v: '100', label: '100' },
              { v: '200', label: '200' },
              { v: '300', label: '300' },
            ]}
          />
          <div className="hint" style={{ marginTop: 4 }}>
            {tr('读得越多越全，用量和时间也越多。', 'More papers: better coverage, more usage and time.')}
          </div>
        </div>
      </div>

      <div className="dr-brief-foot">
        <span className="dr-brief-problem" role={problem ? 'alert' : undefined}>
          {problem ?? ''}
        </span>
        <button type="button" className="btn btn-plain" onClick={onCancel} disabled={busy}>
          {tr('取消', 'Cancel')}
        </button>
        <button type="button" className="btn btn-primary" onClick={submit} disabled={busy || !!problem}>
          {busy ? busyLabel : submitLabel}
        </button>
      </div>
    </div>
  );
}
