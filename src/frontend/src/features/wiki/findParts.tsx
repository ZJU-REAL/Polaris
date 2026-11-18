import { useState, type ReactNode } from 'react';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type AnchorPaper, type ResolvedPaperBatchItem } from '../../lib/api';
import { recognizePaperRef, refInput, refKey, refLabel } from '../../lib/paper-ref';
import { tr } from '../../lib/i18n';
import { splitPaperInput } from './paperInput';

/* ============================================================
   「找论文」页用的几个表单小件：设置行、滑杆、代表论文（锚点）编辑器。
   原来在「检索与同步」页签（IngestTab）里，那个页签并进了「找论文」。
   ============================================================ */

/** 设置行：左边标签（+ 一行说明），右边控件。 */
export function Row({
  label,
  hint,
  title,
  warn,
  children,
}: {
  label: string;
  hint?: string;
  title?: string;
  warn?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="settings-row" style={{ flexWrap: 'wrap', rowGap: 8 }}>
      <div className="settings-row-text" style={{ minWidth: 180 }} title={title}>
        <div style={{ fontSize: 13 }}>{label}</div>
        {hint && (
          <div style={{ fontSize: 12, color: warn ? 'var(--warn-tx)' : 'var(--text-3)', marginTop: 2, lineHeight: 1.5 }}>
            {hint}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

export function KnobRange({
  label,
  hint,
  value,
  min,
  max,
  step,
  format,
  onChange,
  disabled,
  disabledText,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format?: (v: number) => string;
  onChange: (v: number) => void;
  disabled?: boolean;
  disabledText?: string;
}) {
  return (
    <Row label={label} hint={hint}>
      <div className="row gap12" style={{ width: 280, maxWidth: '100%', ...(disabled ? { opacity: 0.45 } : {}) }}>
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={disabled}
          aria-label={label}
          onChange={(e) => onChange(Number(e.target.value))}
          style={{ flex: 1, minWidth: 0 }}
        />
        <span
          className="mono"
          style={{ fontSize: 12, fontWeight: 600, minWidth: 44, textAlign: 'right', whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}
        >
          {disabled && disabledText ? disabledText : format ? format(value) : value}
        </span>
      </div>
    </Row>
  );
}

const MAX_ANCHOR_PAPERS = 50;

/** 草稿 → 认得出的标识（去重）与认不出的原文。标识不限 arXiv（#821）：DOI、PMID 同样可以当锚点。 */
function parseAnchorRefs(raw: string): { refs: string[]; unknown: string[] } {
  const refs: string[] = [];
  const unknown: string[] = [];
  const seen = new Set<string>();
  for (const piece of splitPaperInput(raw)) {
    const ref = recognizePaperRef(piece);
    if (!ref) {
      unknown.push(piece);
      continue;
    }
    const key = refKey(refInput(ref));
    if (!seen.has(key)) {
      seen.add(key);
      refs.push(piece);
    }
  }
  return { refs, unknown };
}

/** 解析结果 → 锚点：按类型落到 arxiv_id / doi / pmid 之一。 */
function anchorFromResolved(raw: string, item: ResolvedPaperBatchItem | undefined): AnchorPaper {
  const ref = recognizePaperRef(raw);
  const base: AnchorPaper = { title: item?.title || '' };
  if (!ref) return base;
  if (ref.kind === 'arxiv') return { ...base, arxiv_id: item?.arxiv_id || ref.value };
  if (ref.kind === 'doi') return { ...base, doi: item?.doi || ref.value };
  return { ...base, pmid: item?.pmid || ref.value };
}

/** 锚点论文编辑器：批量填写 arXiv 编号 / DOI / PMID，题目由系统一次性补上。
 *
 * 从「收录设置」搬到这里——锚点是给「从锚点论文扩展」用的输入，放在收录设置里
 * 与检索/打分的配置混在一起，用户找不到它跟哪个动作有关。 */
export function AnchorEditor({
  libraryId,
  anchors,
  onChange,
  disabled,
}: {
  libraryId?: string;
  anchors: AnchorPaper[];
  onChange: (next: AnchorPaper[]) => void;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);

  async function add() {
    const existing = new Set(anchors.map(refKey));
    const { refs, unknown } = parseAnchorRefs(draft);
    if (unknown.length > 0) {
      toast(
        tr(
          `无法识别：${unknown.join('、')}。请填写 arXiv 编号、DOI 或 PMID`,
          `Couldn’t recognise ${unknown.join(', ')}. Use arXiv IDs, DOIs or PMIDs.`,
        ),
        'error',
      );
      return;
    }
    const ids = refs.filter((raw) => {
      const ref = recognizePaperRef(raw);
      return ref ? !existing.has(refKey(refInput(ref))) : false;
    });
    if (ids.length === 0) {
      toast(tr('已在列表中', 'Already in the list'), 'info');
      return;
    }
    if (anchors.length + ids.length > MAX_ANCHOR_PAPERS) {
      toast(
        tr(
          `锚点论文最多 ${MAX_ANCHOR_PAPERS} 篇，已有 ${anchors.length} 篇`,
          `You can add up to ${MAX_ANCHOR_PAPERS} anchor papers. You have ${anchors.length}.`,
        ),
        'error',
      );
      return;
    }
    setBusy(true);
    try {
      const resolved = await api.resolvePaperRefs(ids);
      const additions = ids.map((raw, index) => anchorFromResolved(raw, resolved.items[index]));
      const failed = resolved.items.filter((item) => item.error).length;
      onChange([...anchors, ...additions]);
      if (failed > 0) {
        toast(
          tr(
            `已添加，其中 ${failed} 篇未找到题目`,
            `Added. Couldn’t find ${failed === 1 ? 'the title for 1 paper' : `titles for ${failed} papers`}.`,
          ),
          'info',
        );
      }
      setDraft('');
    } catch {
      onChange([...anchors, ...ids.map((raw) => anchorFromResolved(raw, undefined))]);
      setDraft('');
      toast(
        tr('已按编号添加，暂时无法查询题目', 'Added by ID. Titles couldn’t be looked up.'),
        'info',
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="col gap8">
      {anchors.length > 0 && (
        <div className="col gap6">
          {anchors.map((a, i) => (
            <div key={`${refKey(a)}-${i}`} className="row gap8" style={{ alignItems: 'center' }}>
              <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
                {refLabel(a)}
              </span>
              <span style={{ fontSize: 12.5, flex: 1, minWidth: 0 }} className="ellipsis">
                {a.title || <span className="muted">{tr('（无题目）', '(no title)')}</span>}
              </span>
              {!disabled && (
                <button
                  type="button"
                  className="btn btn-ghost sm"
                  onClick={() => onChange(anchors.filter((_, j) => j !== i))}
                >
                  <Icon name="x" size={11} />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {!disabled && (
        <div className="col gap8">
          <textarea
            className="textarea mono"
            style={{ width: '100%', minHeight: 72, resize: 'vertical', fontSize: 12 }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                void add();
              }
            }}
            placeholder={tr(
              '例如 2005.11401, 10.1038/s41586-020-2649-2, PMID:31452104',
              'e.g. 2005.11401, 10.1038/s41586-020-2649-2, PMID:31452104',
            )}
          />
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button type="button" className="btn btn-soft sm" disabled={busy || !draft.trim()} onClick={() => void add()}>
              {busy
                ? tr('解析中…', 'Resolving…')
                : parseAnchorRefs(draft).refs.length > 1
                  ? tr(`添加 ${parseAnchorRefs(draft).refs.length} 篇`, `Add ${parseAnchorRefs(draft).refs.length} papers`)
                  : tr('添加', 'Add')}
            </button>
          </div>
        </div>
      )}
      {libraryId && anchors.length === 0 && (
        <div className="muted" style={{ fontSize: 11.5 }}>
          {tr('还没有代表论文。添加几篇这个方向的代表作，例如 2005.11401。', 'No key papers yet. Add a few, e.g. 2005.11401.')}
        </div>
      )}
    </div>
  );
}

