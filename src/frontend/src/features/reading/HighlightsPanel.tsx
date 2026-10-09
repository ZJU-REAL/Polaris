import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import {
  api,
  type HighlightColor,
  type HighlightRead,
  type HighlightStyle,
} from '../../lib/api';
import { HIGHLIGHT_COLORS, HIGHLIGHT_STYLES, highlightColorMeta } from './shared';

/* ============================================================
   阅读工作台 · 标注面板：
   列出本篇全部划线（原文引用 + 颜色 + 批注），点卡片跳回 PDF 对应位置；
   本人可改颜色、写批注、删除。
   ============================================================ */

export interface HighlightsPanelProps {
  paperId: string;
  pid: string;
  highlights: HighlightRead[];
  loading: boolean;
  error: boolean;
  activeHighlightId: string | null;
  onJump: (h: HighlightRead) => void;
  onChanged: () => void;
}

function HighlightCard({
  hl,
  canEdit,
  active,
  onJump,
  onChanged,
}: {
  hl: HighlightRead;
  canEdit: boolean;
  active: boolean;
  onJump: () => void;
  onChanged: () => void;
}) {
  const meta = highlightColorMeta(hl.color);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(hl.note ?? '');
  const cardRef = useRef<HTMLDivElement>(null);

  // 被 PDF 端选中时滚动到可视区
  useEffect(() => {
    if (active) cardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [active]);

  const patchMutation = useMutation({
    mutationFn: (input: { color?: HighlightColor; style?: HighlightStyle; note?: string | null }) =>
      api.patchHighlight(hl.id, input),
    onSuccess: () => onChanged(),
    onError: (e) => toast(`${tr('保存失败：', 'Couldn’t save: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteHighlight(hl.id),
    onSuccess: () => {
      toast(tr('已删除划线', 'Highlight deleted'), 'ok');
      onChanged();
    },
    onError: (e) => toast(`${tr('删除失败：', 'Couldn’t delete: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const saveNote = () => {
    patchMutation.mutate(
      { note: draft.trim() || '' },
      {
        onSuccess: () => {
          toast(tr('已保存批注', 'Note saved'), 'ok');
          setEditing(false);
          onChanged();
        },
      },
    );
  };

  return (
    <div
      ref={cardRef}
      className="card"
      style={{
        padding: '10px 12px',
        marginBottom: 10,
        borderLeft: `3px solid ${meta.solid}`,
        // 选中时只在下方加一条 accent 线（不整框描边）
        boxShadow: active ? 'inset 0 -2px 0 0 var(--accent)' : 'none',
        transition: 'box-shadow 0.15s',
      }}
    >
      {/* 原文引用（点它跳回 PDF） */}
      <div
        onClick={onJump}
        title={tr('跳到 PDF 中的位置', 'Jump to this spot in the PDF')}
        style={{
          fontSize: 13,
          lineHeight: 1.5,
          color: 'var(--text-2)',
          fontStyle: 'italic',
          cursor: 'pointer',
          display: '-webkit-box',
          WebkitLineClamp: 4,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
        }}
      >
        {hl.selected_text}
      </div>

      {/* 批注展示 / 编辑 */}
      {editing ? (
        <div style={{ marginTop: 8 }}>
          <textarea
            className="textarea"
            style={{ width: '100%', minHeight: 60, fontSize: 13, resize: 'vertical' }}
            placeholder={tr('写批注，支持 Markdown', 'Add a note (Markdown supported)')}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            autoFocus
          />
          {/* 改样式 */}
          <div className="row gap6" style={{ marginTop: 6 }}>
            {HIGHLIGHT_STYLES.map((st) => (
              <span
                key={st.v}
                className={`chip${hl.style === st.v ? ' on' : ''}`}
                style={{ fontSize: 11 }}
                onClick={() => patchMutation.mutate({ style: st.v })}
              >
                {tr(st.label, st.en)}
              </span>
            ))}
          </div>
          <div className="row gap8" style={{ marginTop: 6 }}>
            {/* 改颜色 */}
            <span className="row gap6" style={{ marginRight: 'auto' }}>
              {HIGHLIGHT_COLORS.map((c) => (
                <button
                  key={c.v}
                  title={tr(`${c.label}色`, c.en)}
                  onClick={() => patchMutation.mutate({ color: c.v })}
                  style={{
                    width: 16,
                    height: 16,
                    borderRadius: '50%',
                    background: c.solid,
                    border: hl.color === c.v ? '2px solid var(--text)' : '1.5px solid var(--surface)',
                    boxShadow: '0 0 0 1px var(--border-2)',
                    cursor: 'pointer',
                    padding: 0,
                  }}
                />
              ))}
            </span>
            <button className="btn btn-ghost sm" onClick={() => setEditing(false)}>
              {tr('取消', 'Cancel')}
            </button>
            <button className="btn btn-primary sm" disabled={patchMutation.isPending} onClick={saveNote}>
              {tr('保存', 'Save')}
            </button>
          </div>
        </div>
      ) : (
        hl.note && (
          <div style={{ marginTop: 6, paddingTop: 6, borderTop: '0.5px dashed var(--border-2)' }}>
            <Markdown source={hl.note} style={{ fontSize: 12 }} />
          </div>
        )
      )}

      {/* 页脚：页码 · 时间 · 操作（单用户本地版不显示作者） */}
      <div className="row gap8" style={{ marginTop: 7 }}>
        <span className="mono" style={{ fontSize: 10, color: 'var(--text-3)' }}>
          P{hl.page}
        </span>
        <span className="mono" style={{ fontSize: 10, color: 'var(--text-3)' }}>{fmtTime(hl.created_at)}</span>
        {canEdit && !editing && (
          <span className="row gap6" style={{ marginLeft: 'auto' }}>
            <button
              className="icon-btn"
              title={hl.note ? tr('编辑批注', 'Edit note') : tr('添加批注', 'Add note')}
              style={{ width: 22, height: 22 }}
              onClick={() => {
                setDraft(hl.note ?? '');
                setEditing(true);
              }}
            >
              <Icon name="pen" size={11} />
            </button>
            <button
              className="icon-btn"
              title={tr('删除划线', 'Delete highlight')}
              style={{ width: 22, height: 22 }}
              disabled={deleteMutation.isPending}
              onClick={() => deleteMutation.mutate()}
            >
              <Icon name="trash" size={11} />
            </button>
          </span>
        )}
      </div>
    </div>
  );
}

export function HighlightsPanel({
  highlights,
  loading,
  error,
  activeHighlightId,
  onJump,
  onChanged,
}: HighlightsPanelProps) {
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '14px 14px 12px' }}>
        {loading ? (
          <div className="empty">{tr('加载中…', 'Loading…')}</div>
        ) : error ? (
          <EmptyState compact icon="x" title={tr('无法加载划线', 'Couldn’t load highlights')} desc={tr('请确认本机引擎正在运行，然后重试。', 'Make sure the local engine is running, then try again.')} />
        ) : highlights.length === 0 ? (
          <EmptyState
            compact
            icon="pen"
            title={tr('还没有划线', 'No highlights yet')}
            desc={tr('在 PDF 中选中文字，再选一种颜色即可划线。', 'Select text in the PDF, then pick a color to highlight it.')}
          />
        ) : (
          highlights.map((h) => (
            <HighlightCard
              key={h.id}
              hl={h}
              active={h.id === activeHighlightId}
              canEdit={!!me && me.id === h.author_id}
              onJump={() => onJump(h)}
              onChanged={onChanged}
            />
          ))
        )}
      </div>
    </div>
  );
}
