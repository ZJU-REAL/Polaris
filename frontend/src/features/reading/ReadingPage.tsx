import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type MyMeta, type PaperDetail, type ReadingStatus } from '../../lib/api';
import { NotesPanel } from './NotesPanel';
import { ChatPanel } from './ChatPanel';
import { InfoPanel } from './InfoPanel';
import { READING_STATUS } from './shared';

/* ============================================================
   /papers/:id/read — 论文阅读工作台：
   顶栏（返回 / 标题 / 星标 / 阅读状态）
   + 左 PDF（fetch blob → objectURL → iframe）
   + 右侧三面板（笔记 / AI 伴读 / 论文信息）。
   ============================================================ */

type PanelTab = 'notes' | 'chat' | 'info';

/* ---------------- PDF 查看器 ---------------- */

function PdfPane({ paper }: { paper: PaperDetail }) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState<string | null>(null);

  const pdfQuery = useQuery({
    queryKey: ['paper-pdf', paper.id],
    queryFn: () => api.fetchPaperPdf(paper.id),
    retry: false,
    staleTime: Infinity,
  });

  // blob → objectURL；换论文/卸载时 revoke
  useEffect(() => {
    const blob = pdfQuery.data;
    if (!blob) {
      setUrl(null);
      return;
    }
    // 确保 MIME 是 application/pdf——类型缺失/错误时 Chrome 会把 iframe 当下载处理
    const typed = blob.type === 'application/pdf' ? blob : new Blob([blob], { type: 'application/pdf' });
    const u = URL.createObjectURL(typed);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [pdfQuery.data]);

  const fetchPdfMutation = useMutation({
    mutationFn: () => api.requestPaperPdf(paper.id),
    onSuccess: () => {
      toast('PDF 已下载好，正在打开', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['paper-pdf', paper.id] });
      void queryClient.invalidateQueries({ queryKey: ['paper', paper.id] });
    },
    onError: (e) => {
      const msg = e instanceof ApiError && e.message.includes('PDF_FETCH_FAILED')
        ? '下载失败，源站暂时取不到，稍后再试'
        : e instanceof Error ? e.message : String(e);
      toast(`获取 PDF 失败：${msg}`, 'error');
    },
  });

  if (pdfQuery.isLoading) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div
            className="pulse"
            style={{
              width: 220,
              height: 300,
              margin: '0 auto 16px',
              borderRadius: 10,
              background: 'var(--surface-3)',
            }}
          />
          <div className="muted" style={{ fontSize: 12.5 }}>正在加载 PDF…</div>
        </div>
      </div>
    );
  }

  // 无 PDF：引导获取
  if (pdfQuery.isError || !url) {
    const canFetch = !!paper.arxiv_id;
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <EmptyState
          icon="file"
          title="该论文还没有 PDF"
          desc={
            canFetch
              ? '可以从 arXiv 自动下载一份，下载后就能在这里阅读。'
              : '这篇论文不是 arXiv 来源，暂时不支持自动下载 PDF，可以通过右上角原文链接查看。'
          }
          action={
            canFetch ? (
              <button
                className="btn btn-primary"
                disabled={fetchPdfMutation.isPending}
                onClick={() => fetchPdfMutation.mutate()}
              >
                {fetchPdfMutation.isPending ? (
                  <>
                    <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                    正在下载…
                  </>
                ) : (
                  <>
                    <Icon name="download" size={14} />
                    获取 PDF
                  </>
                )}
              </button>
            ) : paper.url ? (
              <a className="btn btn-ghost" href={paper.url} target="_blank" rel="noreferrer noopener" style={{ textDecoration: 'none' }}>
                <Icon name="link" size={14} />
                打开原文链接
              </a>
            ) : undefined
          }
        />
      </div>
    );
  }

  return <iframe src={url} title="论文 PDF" style={{ flex: 1, width: '100%', border: 'none', background: '#525659' }} />;
}

/* ---------------- 页面 ---------------- */

export function ReadingPage() {
  const { id = '' } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [panel, setPanel] = useState<PanelTab>('notes');

  const paperQuery = useQuery({
    queryKey: ['paper', id],
    queryFn: () => api.getPaper(id),
    enabled: !!id,
    retry: false,
  });
  const paper = paperQuery.data;

  // 星标 / 阅读状态（乐观更新详情缓存，列表页缓存失效）
  const metaMutation = useMutation({
    mutationFn: (input: Partial<MyMeta>) => api.putMyMeta(id, input),
    onSuccess: (meta) => {
      queryClient.setQueryData<PaperDetail>(['paper', id], (old) =>
        old ? { ...old, starred: meta.starred, reading_status: meta.reading_status } : old,
      );
      if (paper) void queryClient.invalidateQueries({ queryKey: ['papers', paper.project_id] });
    },
    onError: (e) => toast(`更新失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const onWikiLink = useCallback(
    (name: string) => navigate(`/wiki?concept=${encodeURIComponent(name)}`),
    [navigate],
  );

  if (paperQuery.isLoading) {
    return <div className="empty" style={{ marginTop: 120 }}>加载论文…</div>;
  }
  if (paperQuery.isError || !paper) {
    return (
      <div style={{ marginTop: 100 }}>
        <EmptyState
          icon="x"
          title="打不开这篇论文"
          desc="论文不存在、你不在这个研究方向里，或后端暂时不可用。"
          action={
            <button className="btn btn-ghost" onClick={() => navigate('/wiki')}>
              <Icon name="book" size={14} />
              回文献库
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
          回文献库
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
            {paper.arxiv_id ?? paper.venue ?? '论文阅读 · Reading'}
          </div>
        </div>
        <button
          className="icon-btn"
          title={starred ? '取消星标' : '加星标'}
          disabled={metaMutation.isPending}
          onClick={() => metaMutation.mutate({ starred: !starred })}
          style={{ color: starred ? 'var(--warn-tx)' : 'var(--text-3)' }}
        >
          <Icon name={starred ? 'starFill' : 'star'} size={17} />
        </button>
        <Segmented<ReadingStatus>
          options={READING_STATUS.map((m) => ({ v: m.v, label: m.label }))}
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
        {/* 左：PDF */}
        <div
          className="card"
          style={{ minWidth: 0, minHeight: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
        >
          <PdfPane paper={paper} />
        </div>

        {/* 右：三面板 */}
        <div
          className="card"
          style={{ minWidth: 0, minHeight: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
        >
          <div style={{ padding: '10px 14px 0', flexShrink: 0 }}>
            <Segmented<PanelTab>
              options={[
                { v: 'notes', label: `笔记${paper.note_count ? ` · ${paper.note_count}` : ''}` },
                { v: 'chat', label: 'AI 伴读' },
                { v: 'info', label: '论文信息' },
              ]}
              value={panel}
              onChange={setPanel}
            />
          </div>
          {panel === 'notes' ? (
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
