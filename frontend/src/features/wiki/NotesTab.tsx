import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { api, type NoteWithPaper } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { SearchInput, useDebounced } from './shared';

/* ============================================================
   笔记本 Tab：整个研究方向的阅读笔记（搜索 + 分页），
   点论文标题跳该论文的阅读页。
   ============================================================ */

const PAGE_SIZE = 20;

function NoteItem({ note, onOpenPaper }: { note: NoteWithPaper; onOpenPaper: () => void }) {
  const edited = note.updated_at && note.updated_at !== note.created_at;
  return (
    <div className="card" style={{ padding: '13px 16px' }}>
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        <span style={{ fontSize: 12.5, fontWeight: 650 }}>{note.author_name}</span>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
          {fmtTime(note.created_at)}
          {edited ? ` · ${tr('已编辑', 'edited')}` : ''}
        </span>
        <span
          className="row gap6"
          onClick={onOpenPaper}
          title={tr('打开这篇论文的阅读页', 'Open the reading page for this paper')}
          style={{
            marginLeft: 'auto',
            minWidth: 0,
            maxWidth: '55%',
            cursor: 'pointer',
            color: 'var(--accent-text)',
            fontSize: 11.5,
            fontWeight: 600,
          }}
        >
          <Icon name="book" size={12} style={{ flexShrink: 0 }} />
          <span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {note.paper_title}
          </span>
          <Icon name="chevron" size={11} style={{ flexShrink: 0 }} />
        </span>
      </div>
      <Markdown source={note.content} style={{ fontSize: 12.5 }} />
    </div>
  );
}

export function NotesTab({ pid }: { pid: string }) {
  const navigate = useNavigate();
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  const [page, setPage] = useState(1);

  // 搜索词变化时回到第一页
  useEffect(() => {
    setPage(1);
  }, [q]);

  const notesQuery = useQuery({
    queryKey: ['project-notes', pid, q, page],
    queryFn: () => api.listProjectNotes(pid, { q: q || undefined, page, size: PAGE_SIZE }),
    retry: false,
    placeholderData: keepPreviousData,
  });

  const data = notesQuery.data;
  const notes = data?.items ?? [];
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* —— 工具栏 —— */}
      <div
        className="row gap12"
        style={{ padding: '12px 16px', borderBottom: '0.5px solid var(--border)', flexShrink: 0 }}
      >
        <div style={{ maxWidth: 380, flex: 1 }}>
          <SearchInput value={qInput} onChange={setQInput} placeholder={tr('搜索笔记内容…', 'Search notes…')} />
        </div>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginLeft: 'auto' }}>
          {data ? tr(`共 ${data.total} 条`, `${data.total} total`) : ''}
        </span>
      </div>

      {/* —— 列表 —— */}
      <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '16px 20px' }}>
        {notesQuery.isLoading ? (
          <div className="empty">{tr('加载笔记…', 'Loading notes…')}</div>
        ) : notesQuery.isError ? (
          <EmptyState
            compact
            icon="x"
            title={tr('笔记暂时加载不出来', 'Notes failed to load')}
            desc={tr('后端不可用或接口尚未就绪，稍后再试。', 'Backend unavailable or API not ready — try again later.')}
          />
        ) : notes.length === 0 ? (
          <EmptyState
            compact
            icon="pen"
            title={q ? tr('没有匹配的笔记', 'No matching notes') : tr('还没有笔记', 'No notes yet')}
            desc={
              q
                ? tr('换个关键词试试。', 'Try a different keyword.')
                : tr(
                    '打开一篇论文的阅读页，在右侧笔记面板写下第一条笔记。',
                    'Open a paper reading page and write your first note in the notes panel.',
                  )
            }
          />
        ) : (
          <div className="col" style={{ gap: 12, maxWidth: 860, margin: '0 auto' }}>
            {notes.map((n) => (
              <NoteItem key={n.id} note={n} onOpenPaper={() => navigate(`/papers/${n.paper_id}/read`)} />
            ))}
          </div>
        )}
      </div>

      {/* —— 分页 —— */}
      {data && data.total > PAGE_SIZE && (
        <div
          className="row gap12"
          style={{
            padding: '10px 16px',
            borderTop: '0.5px solid var(--border)',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <button className="btn btn-ghost sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            <Icon name="chevron" size={12} style={{ transform: 'rotate(180deg)' }} />
            {tr('上一页', 'Prev')}
          </button>
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
            {tr(`第 ${page} / ${totalPages} 页`, `Page ${page} / ${totalPages}`)}
          </span>
          <button
            className="btn btn-ghost sm"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            {tr('下一页', 'Next')}
            <Icon name="chevron" size={12} />
          </button>
        </div>
      )}
    </div>
  );
}
