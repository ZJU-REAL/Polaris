import { useCallback, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import {
  api,
  type HighlightCreateInput,
  type HighlightRead,
  type MyMeta,
  type PaperDetail,
  type ReadingStatus,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { NotesPanel } from './NotesPanel';
import { HighlightsPanel } from './HighlightsPanel';
import { PdfReader, type JumpTarget } from './PdfReader';
import { ChatPanel } from './ChatPanel';
import { InfoPanel } from './InfoPanel';
import { READING_STATUS } from './shared';

/* ============================================================
   /papers/:id/read — 论文阅读工作台：
   顶栏（返回 / 标题 / 星标 / 阅读状态）
   + 左：自建 PDF 阅读器（pdf.js，可划线）
   + 右：四面板（标注 / 笔记 / AI 伴读 / 论文信息）。
   划线与右侧标注列表共享选中态，可互相跳转。
   ============================================================ */

type PanelTab = 'highlights' | 'notes' | 'chat' | 'info';

export function ReadingPage() {
  const { id = '' } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [panel, setPanel] = useState<PanelTab>('highlights');
  const [activeHl, setActiveHl] = useState<string | null>(null);
  const [jump, setJump] = useState<JumpTarget | null>(null);

  const paperQuery = useQuery({
    queryKey: ['paper', id],
    queryFn: () => api.getPaper(id),
    enabled: !!id,
    retry: false,
  });
  const paper = paperQuery.data;

  const highlightsQuery = useQuery({
    queryKey: ['paper-highlights', id],
    queryFn: () => api.listPaperHighlights(id),
    enabled: !!id,
    retry: false,
  });
  const highlights = highlightsQuery.data ?? [];

  const invalidateHighlights = useCallback(
    () => void queryClient.invalidateQueries({ queryKey: ['paper-highlights', id] }),
    [queryClient, id],
  );

  const createHlMutation = useMutation({
    mutationFn: (input: HighlightCreateInput) => api.createPaperHighlight(id, input),
    onSuccess: (created) => {
      setActiveHl(created.id);
      setPanel('highlights');
      invalidateHighlights();
    },
    onError: (e) => toast(`${tr('划线失败：', 'Highlight failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // 星标 / 阅读状态（乐观更新详情缓存，列表页缓存失效）
  const metaMutation = useMutation({
    mutationFn: (input: Partial<MyMeta>) => api.putMyMeta(id, input),
    onSuccess: (meta) => {
      queryClient.setQueryData<PaperDetail>(['paper', id], (old) =>
        old ? { ...old, starred: meta.starred, reading_status: meta.reading_status } : old,
      );
      if (paper) void queryClient.invalidateQueries({ queryKey: ['papers', paper.project_id] });
    },
    onError: (e) => toast(`${tr('更新失败：', 'Update failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const onWikiLink = useCallback(
    (name: string) => navigate(`/wiki?concept=${encodeURIComponent(name)}`),
    [navigate],
  );

  const onHighlightClick = useCallback((hlId: string) => {
    setActiveHl(hlId);
    setPanel('highlights');
  }, []);

  const onJump = useCallback((h: HighlightRead) => {
    setActiveHl(h.id);
    setJump({ id: h.id, page: h.page, nonce: performance.now() });
  }, []);

  if (paperQuery.isLoading) {
    return <div className="empty" style={{ marginTop: 120 }}>{tr('加载论文…', 'Loading paper…')}</div>;
  }
  if (paperQuery.isError || !paper) {
    return (
      <div style={{ marginTop: 100 }}>
        <EmptyState
          icon="x"
          title={tr('打不开这篇论文', 'Cannot open this paper')}
          desc={tr('论文不存在、你不在这个研究方向里，或后端暂时不可用。', 'It does not exist, you are not in this research direction, or the backend is unavailable.')}
          action={
            <button className="btn btn-ghost" onClick={() => navigate('/wiki')}>
              <Icon name="book" size={14} />
              {tr('回文献库', 'Back to library')}
            </button>
          }
        />
      </div>
    );
  }

  const starred = paper.starred ?? false;
  const readingStatus: ReadingStatus = paper.reading_status ?? 'unread';

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', padding: '14px 18px 16px' }}>
      {/* —— 顶栏 —— */}
      <div className="row gap12" style={{ flexShrink: 0, marginBottom: 12 }}>
        <button className="btn btn-ghost sm" onClick={() => navigate(`/wiki?paper=${paper.id}`)}>
          <Icon name="chevron" size={13} style={{ transform: 'rotate(180deg)' }} />
          {tr('回文献库', 'Back to library')}
        </button>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            title={paper.title}
            style={{
              fontSize: 14.5,
              fontWeight: 660,
              letterSpacing: '-0.01em',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {paper.title}
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 1 }}>
            {paper.arxiv_id ?? paper.venue ?? tr('论文阅读', 'Paper reading')}
          </div>
        </div>
        <button
          className="icon-btn"
          title={starred ? tr('取消星标', 'Unstar') : tr('加星标', 'Star')}
          disabled={metaMutation.isPending}
          onClick={() => metaMutation.mutate({ starred: !starred })}
          style={{ color: starred ? 'var(--warn-tx)' : 'var(--text-3)' }}
        >
          <Icon name={starred ? 'starFill' : 'star'} size={17} />
        </button>
        <Segmented<ReadingStatus>
          options={READING_STATUS.map((m) => ({ v: m.v, label: tr(m.label, m.en) }))}
          value={readingStatus}
          onChange={(v) => metaMutation.mutate({ reading_status: v })}
        />
      </div>

      {/* —— 左右分栏 —— */}
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: 'grid',
          gridTemplateColumns: '68fr 32fr',
          gap: 14,
        }}
      >
        {/* 左：PDF 阅读器（可划线） */}
        <div
          className="card"
          style={{ minWidth: 0, minHeight: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
        >
          <PdfReader
            paper={paper}
            highlights={highlights}
            activeHighlightId={activeHl}
            creating={createHlMutation.isPending}
            onCreateHighlight={(input) => createHlMutation.mutate(input)}
            onHighlightClick={onHighlightClick}
            jumpTarget={jump}
          />
        </div>

        {/* 右：四面板 */}
        <div
          className="card"
          style={{ minWidth: 0, minHeight: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
        >
          <div style={{ padding: '10px 14px 0', flexShrink: 0 }}>
            <Segmented<PanelTab>
              options={[
                { v: 'highlights', label: `${tr('标注', 'Highlights')}${highlights.length ? ` · ${highlights.length}` : ''}` },
                { v: 'notes', label: `${tr('笔记', 'Notes')}${paper.note_count ? ` · ${paper.note_count}` : ''}` },
                { v: 'chat', label: tr('AI 伴读', 'AI chat') },
                { v: 'info', label: tr('论文信息', 'Paper info') },
              ]}
              value={panel}
              onChange={setPanel}
            />
          </div>
          {panel === 'highlights' ? (
            <HighlightsPanel
              paperId={paper.id}
              pid={paper.project_id}
              highlights={highlights}
              loading={highlightsQuery.isLoading}
              error={highlightsQuery.isError}
              activeHighlightId={activeHl}
              onJump={onJump}
              onChanged={invalidateHighlights}
            />
          ) : panel === 'notes' ? (
            <NotesPanel paperId={paper.id} pid={paper.project_id} />
          ) : panel === 'chat' ? (
            <ChatPanel paperId={paper.id} pid={paper.project_id} />
          ) : (
            <InfoPanel paper={paper} onWikiLink={onWikiLink} />
          )}
        </div>
      </div>
    </div>
  );
}
