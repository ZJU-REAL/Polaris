import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { api, isAdmin, type HighlightColor, type HighlightRead } from '../../lib/api';
import { HIGHLIGHT_COLORS, highlightColorMeta } from './shared';

/* ============================================================
   阅读工作台 · 标注面板：
   列出本篇全部划线（原文引用 + 颜色 + 批注），点卡片跳回 PDF 对应位置；
   本人 / 管理员可改颜色、写批注、删除。
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
    mutationFn: (input: { color?: HighlightColor; note?: string | null }) =>
      api.patchHighlight(hl.id, input),
    onSuccess: () => onChanged(),
    onError: (e) => toast(`保存失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteHighlight(hl.id),
    onSuccess: () => {
      toast('划线已删除', 'ok');
      onChanged();
    },
    onError: (e) => toast(`删除失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const saveNote = () => {
    patchMutation.mutate(
      { note: draft.trim() || '' },
      {
        onSuccess: () => {
          toast('批注已保存', 'ok');
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
        outline: active ? '1.5px solid var(--accent)' : 'none',
        transition: 'outline-color 0.15s',
      }}
    >
      {/* 原文引用（点它跳回 PDF） */}
      <div
        onClick={onJump}
        title="跳到 PDF 中的位置"
        style={{
          fontSize: 12.5,
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
        「{hl.selected_text}」
      </div>

      {/* 批注展示 / 编辑 */}
      {editing ? (
        <div style={{ marginTop: 8 }}>
          <textarea
            className="textarea"
            style={{ width: '100%', minHeight: 60, fontSize: 12.5, resize: 'vertical' }}
            placeholder="给这处划线写点批注，支持 Markdown…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            autoFocus
          />
          <div className="row gap8" style={{ marginTop: 6 }}>
            {/* 改颜色 */}
            <span className="row gap6" style={{ marginRight: 'auto' }}>
              {HIGHLIGHT_COLORS.map((c) => (
                <button
                  key={c.v}
                  title={`${c.label}色`}
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
              取消
            </button>
            <button className="btn btn-primary sm" disabled={patchMutation.isPending} onClick={saveNote}>
              保存
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

      {/* 页脚：页码 · 作者 · 时间 · 操作 */}
      <div className="row gap8" style={{ marginTop: 7 }}>
        <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>
          P{hl.page}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{hl.author_name}</span>
        <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>{fmtTime(hl.created_at)}</span>
        {canEdit && !editing && (
          <span className="row gap6" style={{ marginLeft: 'auto' }}>
            <button
              className="icon-btn"
              title={hl.note ? '编辑批注' : '加批注'}
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
              title="删除划线"
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
          <div className="empty">加载标注…</div>
        ) : error ? (
          <EmptyState compact icon="x" title="标注暂时加载不出来" desc="后端不可用或接口尚未就绪，稍后再试。" />
        ) : highlights.length === 0 ? (
          <EmptyState
            compact
            icon="pen"
            title="还没有划线"
            desc="在左边 PDF 里选中句子，点弹出的颜色即可划线，之后能在这里加批注。"
          />
        ) : (
          highlights.map((h) => (
            <HighlightCard
              key={h.id}
              hl={h}
              active={h.id === activeHighlightId}
              canEdit={!!me && (isAdmin(me) || me.id === h.author_id)}
              onJump={() => onJump(h)}
              onChanged={onChanged}
            />
          ))
        )}
      </div>
    </div>
  );
}
