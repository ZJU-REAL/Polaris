import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { api, isAdmin, type NoteRead } from '../../lib/api';

/* ============================================================
   阅读工作台 · 笔记面板：
   笔记列表（作者 + 时间 + markdown，本人/管理员可编辑删除）
   + 底部编辑器（textarea / 预览切换 / 发布）。
   ============================================================ */

export interface NotesPanelProps {
  paperId: string;
  pid: string;
}

/** 失效所有与笔记相关的缓存（列表 / 笔记本 / 论文 note_count）。 */
function invalidateNotes(qc: ReturnType<typeof useQueryClient>, paperId: string, pid: string) {
  void qc.invalidateQueries({ queryKey: ['paper-notes', paperId] });
  void qc.invalidateQueries({ queryKey: ['project-notes', pid] });
  void qc.invalidateQueries({ queryKey: ['paper', paperId] });
  void qc.invalidateQueries({ queryKey: ['papers', pid] });
}

function NoteCard({
  note,
  canEdit,
  onSaved,
}: {
  note: NoteRead;
  canEdit: boolean;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(note.content);

  const saveMutation = useMutation({
    mutationFn: () => api.patchNote(note.id, draft.trim()),
    onSuccess: () => {
      toast('笔记已更新', 'ok');
      setEditing(false);
      onSaved();
    },
    onError: (e) => toast(`保存失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteNote(note.id),
    onSuccess: () => {
      toast('笔记已删除', 'ok');
      onSaved();
    },
    onError: (e) => toast(`删除失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const edited = note.updated_at && note.updated_at !== note.created_at;

  return (
    <div className="card" style={{ padding: '11px 14px', marginBottom: 10 }}>
      <div className="row gap8" style={{ marginBottom: 7 }}>
        <span style={{ fontSize: 12, fontWeight: 650, color: 'var(--text)' }}>{note.author_name}</span>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
          {fmtTime(note.created_at)}
          {edited ? ' · 已编辑' : ''}
        </span>
        {canEdit && !editing && (
          <span className="row gap6" style={{ marginLeft: 'auto' }}>
            <button
              className="icon-btn"
              title="编辑笔记"
              style={{ width: 24, height: 24 }}
              onClick={() => {
                setDraft(note.content);
                setEditing(true);
              }}
            >
              <Icon name="pen" size={12} />
            </button>
            <button
              className="icon-btn"
              title="删除笔记"
              style={{ width: 24, height: 24 }}
              disabled={deleteMutation.isPending}
              onClick={() => {
                if (window.confirm('删除这条笔记？删掉就找不回来了。')) deleteMutation.mutate();
              }}
            >
              <Icon name="trash" size={12} />
            </button>
          </span>
        )}
      </div>
      {editing ? (
        <>
          <textarea
            className="textarea"
            style={{ width: '100%', minHeight: 90, fontSize: 12.5, resize: 'vertical' }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="row gap8" style={{ marginTop: 8, justifyContent: 'flex-end' }}>
            <button className="btn btn-ghost sm" onClick={() => setEditing(false)}>
              取消
            </button>
            <button
              className="btn btn-primary sm"
              disabled={saveMutation.isPending || !draft.trim()}
              onClick={() => saveMutation.mutate()}
            >
              保存
            </button>
          </div>
        </>
      ) : (
        <Markdown source={note.content} style={{ fontSize: 12.5 }} />
      )}
    </div>
  );
}

export function NotesPanel({ paperId, pid }: NotesPanelProps) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [preview, setPreview] = useState(false);

  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });

  const notesQuery = useQuery({
    queryKey: ['paper-notes', paperId],
    queryFn: () => api.listPaperNotes(paperId),
    retry: false,
  });

  const createMutation = useMutation({
    mutationFn: () => api.createPaperNote(paperId, draft.trim()),
    onSuccess: () => {
      toast('笔记已发布', 'ok');
      setDraft('');
      setPreview(false);
      invalidateNotes(queryClient, paperId, pid);
    },
    onError: (e) => toast(`发布失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const onSaved = () => invalidateNotes(queryClient, paperId, pid);
  const notes = notesQuery.data ?? [];

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* —— 列表 —— */}
      <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '14px 14px 6px' }}>
        {notesQuery.isLoading ? (
          <div className="empty">加载笔记…</div>
        ) : notesQuery.isError ? (
          <EmptyState compact icon="x" title="笔记暂时加载不出来" desc="后端不可用或接口尚未就绪，稍后再试。" />
        ) : notes.length === 0 ? (
          <EmptyState
            compact
            icon="pen"
            title="还没有笔记"
            desc="在下方写下第一条阅读笔记，支持 Markdown 格式。"
          />
        ) : (
          notes.map((n) => (
            <NoteCard
              key={n.id}
              note={n}
              canEdit={!!me && (isAdmin(me) || me.id === n.author_id)}
              onSaved={onSaved}
            />
          ))
        )}
      </div>

      {/* —— 编辑器 —— */}
      <div style={{ borderTop: '0.5px solid var(--border)', padding: '10px 14px 12px', flexShrink: 0 }}>
        <div className="row" style={{ marginBottom: 8, justifyContent: 'space-between' }}>
          <span style={{ fontSize: 12, fontWeight: 650 }}>
            写笔记 <span className="en-label" style={{ fontSize: 10.5 }}>New note</span>
          </span>
          <span className="row gap6">
            <span
              className={`chip${!preview ? ' on' : ''}`}
              style={{ fontSize: 11 }}
              onClick={() => setPreview(false)}
            >
              编辑
            </span>
            <span
              className={`chip${preview ? ' on' : ''}`}
              style={{ fontSize: 11 }}
              onClick={() => setPreview(true)}
            >
              预览
            </span>
          </span>
        </div>
        {preview ? (
          <div
            className="scroll"
            style={{
              minHeight: 88,
              maxHeight: 180,
              overflowY: 'auto',
              border: '0.5px solid var(--border-2)',
              borderRadius: 9,
              padding: '8px 11px',
              background: 'var(--surface-2)',
            }}
          >
            {draft.trim() ? (
              <Markdown source={draft} style={{ fontSize: 12.5 }} />
            ) : (
              <span className="muted" style={{ fontSize: 12 }}>
                （还没写内容，切回「编辑」开始写）
              </span>
            )}
          </div>
        ) : (
          <textarea
            className="textarea"
            style={{ width: '100%', minHeight: 88, maxHeight: 180, fontSize: 12.5, resize: 'vertical' }}
            placeholder="记下想法、疑问或要点，支持 Markdown…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
        )}
        <div className="row" style={{ marginTop: 8, justifyContent: 'flex-end' }}>
          <button
            className="btn btn-primary sm"
            disabled={createMutation.isPending || !draft.trim()}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? (
              <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
            ) : (
              <Icon name="pen" size={13} />
            )}
            发布笔记
          </button>
        </div>
      </div>
    </div>
  );
}
