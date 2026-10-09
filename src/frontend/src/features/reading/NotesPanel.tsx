import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { api, type NoteRead } from '../../lib/api';
import { tr } from '../../lib/i18n';

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

/** 单条笔记卡片（作者 + 时间 + markdown，可就地编辑/删除）。 */
export function NoteCard({
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
      toast(tr('已保存', 'Saved'), 'ok');
      setEditing(false);
      onSaved();
    },
    onError: (e) => toast(`${tr('保存失败：', 'Couldn’t save: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteNote(note.id),
    onSuccess: () => {
      toast(tr('已删除笔记', 'Note deleted'), 'ok');
      onSaved();
    },
    onError: (e) => toast(`${tr('删除失败：', 'Couldn’t delete: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const edited = note.updated_at && note.updated_at !== note.created_at;

  return (
    <div className="card" style={{ padding: '11px 14px', marginBottom: 10 }}>
      <div className="row gap8" style={{ marginBottom: 7 }}>
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
          {fmtTime(note.created_at)}
          {edited ? ` · ${tr('已编辑', 'Edited')}` : ''}
        </span>
        {canEdit && !editing && (
          <span className="row gap6" style={{ marginLeft: 'auto' }}>
            <button
              className="icon-btn"
              title={tr('编辑笔记', 'Edit note')}
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
              title={tr('删除笔记', 'Delete note')}
              style={{ width: 24, height: 24 }}
              disabled={deleteMutation.isPending}
              onClick={() => deleteMutation.mutate()}
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
            style={{ width: '100%', minHeight: 90, fontSize: 13, resize: 'vertical' }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="row gap8" style={{ marginTop: 8, justifyContent: 'flex-end' }}>
            <button className="btn btn-ghost sm" onClick={() => setEditing(false)}>
              {tr('取消', 'Cancel')}
            </button>
            <button
              className="btn btn-primary sm"
              disabled={saveMutation.isPending || !draft.trim()}
              onClick={() => saveMutation.mutate()}
            >
              {tr('保存', 'Save')}
            </button>
          </div>
        </>
      ) : (
        <Markdown source={note.content} style={{ fontSize: 13 }} />
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
      toast(tr('已保存笔记', 'Note saved'), 'ok');
      setDraft('');
      setPreview(false);
      invalidateNotes(queryClient, paperId, pid);
    },
    onError: (e) => toast(`${tr('保存失败：', 'Couldn’t save: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const onSaved = () => invalidateNotes(queryClient, paperId, pid);
  const notes = notesQuery.data ?? [];

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* —— 列表 —— */}
      <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '14px 14px 6px' }}>
        {notesQuery.isLoading ? (
          <div className="empty">{tr('加载中…', 'Loading…')}</div>
        ) : notesQuery.isError ? (
          <EmptyState compact icon="x" title={tr('无法加载笔记', 'Couldn’t load notes')} desc={tr('请确认本机引擎正在运行，然后重试。', 'Make sure the local engine is running, then try again.')} />
        ) : notes.length === 0 ? (
          <EmptyState
            compact
            icon="pen"
            title={tr('还没有笔记', 'No notes yet')}
          />
        ) : (
          notes.map((n) => (
            <NoteCard
              key={n.id}
              note={n}
              canEdit={!!me && me.id === n.author_id}
              onSaved={onSaved}
            />
          ))
        )}
      </div>

      {/* —— 编辑器 —— */}
      <div style={{ borderTop: '0.5px solid var(--border)', padding: '10px 14px 12px', flexShrink: 0 }}>
        <div className="row" style={{ marginBottom: 8, justifyContent: 'space-between' }}>
          <span style={{ fontSize: 12, fontWeight: 600 }}>
            {tr('写笔记', 'New note')}
          </span>
          <span className="row gap6">
            <span
              className={`chip${!preview ? ' on' : ''}`}
              style={{ fontSize: 11 }}
              onClick={() => setPreview(false)}
            >
              {tr('编辑', 'Edit')}
            </span>
            <span
              className={`chip${preview ? ' on' : ''}`}
              style={{ fontSize: 11 }}
              onClick={() => setPreview(true)}
            >
              {tr('预览', 'Preview')}
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
              <Markdown source={draft} style={{ fontSize: 13 }} />
            ) : (
              <span className="muted" style={{ fontSize: 12 }}>
                {tr('暂无内容', 'Nothing to preview')}
              </span>
            )}
          </div>
        ) : (
          <textarea
            className="textarea"
            style={{ width: '100%', minHeight: 88, maxHeight: 180, fontSize: 13, resize: 'vertical' }}
            placeholder={tr('记下想法或疑问，支持 Markdown', 'Ideas or questions (Markdown supported)')}
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
            {tr('保存笔记', 'Save note')}
          </button>
        </div>
      </div>
    </div>
  );
}
