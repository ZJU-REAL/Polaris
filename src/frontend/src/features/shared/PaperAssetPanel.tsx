import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import {
  api,
  ApiError,
  type PaperAssetRead,
  type PaperContentVersionRead,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { StateDot } from './StateDot';

const ACTIVE_PARSE_STATES = new Set([
  'queued',
  'mineru_uploading',
  'mineru_acceptance_wait',
  'mineru_processing',
  'mineru_downloading',
  'parsing',
  'fallback_parsing',
]);

export interface AssetStateMeta {
  label: string;
  tone: 'neutral' | 'accent' | 'warning' | 'success' | 'danger';
  /** 具体在哪个解析阶段（MinerU / PyMuPDF 兜底等），只放进悬停提示，不占标签文案。 */
  detail?: string;
}

export function parseStateMeta(status?: string | null): AssetStateMeta {
  switch (status) {
    case 'queued':
      return { label: tr('排队中', 'Queued'), tone: 'warning' };
    case 'mineru_uploading':
      return { label: tr('正在提取全文', 'Extracting text'), tone: 'accent', detail: tr('正在上传到 MinerU', 'Uploading to MinerU') };
    case 'mineru_acceptance_wait':
      return { label: tr('正在提取全文', 'Extracting text'), tone: 'accent', detail: tr('等待 MinerU 接收', 'Waiting for MinerU') };
    case 'mineru_processing':
      return { label: tr('正在提取全文', 'Extracting text'), tone: 'accent', detail: tr('MinerU 解析中', 'MinerU is parsing') };
    case 'mineru_downloading':
      return { label: tr('正在提取全文', 'Extracting text'), tone: 'accent', detail: tr('正在获取 MinerU 结果', 'Fetching the MinerU result') };
    case 'parsing':
      return { label: tr('正在提取全文', 'Extracting text'), tone: 'accent' };
    case 'fallback_parsing':
      return { label: tr('正在提取全文', 'Extracting text'), tone: 'warning', detail: tr('使用 PyMuPDF 解析', 'Parsing with PyMuPDF') };
    case 'ready':
      return { label: tr('有全文', 'Full text ready'), tone: 'success' };
    case 'ready_fallback':
      return { label: tr('有全文（纯文本）', 'Full text ready (plain)'), tone: 'success' };
    case 'vector_ready':
      return { label: tr('有全文', 'Full text ready'), tone: 'success' };
    case 'failed':
      return { label: tr('解析失败', 'Parsing failed'), tone: 'danger' };
    default:
      return { label: tr('无全文', 'No full text'), tone: 'neutral' };
  }
}

export function vectorStateMeta(state?: string | null): AssetStateMeta {
  switch (state) {
    case 'ready':
      return { label: tr('可语义检索', 'Searchable'), tone: 'success' };
    case 'building':
      return { label: tr('正在建立索引', 'Indexing'), tone: 'accent' };
    case 'pending':
      return { label: tr('等待建立索引', 'Waiting to index'), tone: 'warning' };
    case 'failed':
      return { label: tr('索引失败', 'Indexing failed'), tone: 'danger' };
    default:
      return { label: tr('未建立索引', 'Not indexed'), tone: 'neutral' };
  }
}

/** 论文级与分块级两个索引合成一个状态：先报最需要关注的那个。 */
function combinedVectorState(a?: string | null, b?: string | null): string | null {
  for (const s of ['failed', 'building', 'pending']) {
    if (a === s || b === s) return s;
  }
  if (a === 'ready' && b === 'ready') return 'ready';
  return a === 'ready' || b === 'ready' ? 'pending' : null;
}

function formatBytes(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(value >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
}

function sourceLabel(source: string): string {
  const labels: Record<string, string> = {
    oa: 'OA',
    upload: tr('上传', 'Uploaded'),
    extension: tr('浏览器扩展', 'Browser extension'),
    arxiv: 'arXiv',
    manual: tr('手动添加', 'Added manually'),
  };
  return labels[source] ?? source;
}

function savePdf(blob: Blob, paperId: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${paperId}.pdf`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function PaperAssetPanel({
  libraryId,
  paperId,
  doi,
}: {
  libraryId: string;
  paperId: string;
  doi?: string | null;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  // 单用户本地版不再让人选共享范围，上传一律按「本库」。
  const sharingScope = 'library' as const;
  const [reparseAsset, setReparseAsset] = useState<PaperAssetRead | null>(null);

  const assetsQuery = useQuery({
    queryKey: ['paper-assets', libraryId, paperId],
    queryFn: () => api.listLibraryPaperAssets(libraryId, paperId),
    retry: false,
  });
  const versionQuery = useQuery({
    queryKey: ['paper-content-version', libraryId, paperId],
    queryFn: async () => {
      try {
        return await api.getLibraryPaperContentVersion(libraryId, paperId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    retry: false,
    refetchInterval: (query) => {
      const value = query.state.data as PaperContentVersionRead | null | undefined;
      return value && (ACTIVE_PARSE_STATES.has(value.status) || value.document_vector_state === 'building' || value.chunk_vector_state === 'building')
        ? 2_500
        : false;
    },
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['paper-assets', libraryId, paperId] });
    void queryClient.invalidateQueries({ queryKey: ['paper-content-version', libraryId, paperId] });
    void queryClient.invalidateQueries({ queryKey: ['paper-structured-content', libraryId, paperId] });
    void queryClient.invalidateQueries({ queryKey: ['paper', libraryId, paperId] });
    void queryClient.invalidateQueries({ queryKey: ['papers', libraryId] });
  };

  const uploadMutation = useMutation({
    mutationFn: async (file: File) => {
      const asset = await api.uploadLibraryPaperAsset(libraryId, paperId, file, {
        sharingScope,
        identityKey: doi ? `doi:${doi.toLowerCase()}` : null,
      });
      const version = await api.createLibraryPaperContentVersion(libraryId, paperId, asset.id);
      return { asset, version };
    },
    onSuccess: () => {
      invalidate();
      toast(tr('PDF 已上传，正在解析', 'PDF uploaded. Parsing now.'), 'ok');
    },
    onError: (error) => {
      invalidate();
      toast(`${tr('PDF 上传失败：', 'Couldn’t upload the PDF: ')}${error instanceof Error ? error.message : String(error)}`, 'error');
    },
  });

  const parseMutation = useMutation({
    mutationFn: (assetId: string) => api.createLibraryPaperContentVersion(libraryId, paperId, assetId),
    onSuccess: () => {
      setReparseAsset(null);
      invalidate();
      toast(tr('已开始重新解析', 'Reparsing started'), 'ok');
    },
    onError: (error) => toast(`${tr('无法开始解析：', 'Couldn’t start parsing: ')}${error instanceof Error ? error.message : String(error)}`, 'error'),
  });

  const downloadMutation = useMutation({
    mutationFn: (asset: PaperAssetRead) => api.downloadLibraryPaperAsset(libraryId, paperId, asset.id),
    onSuccess: (blob) => savePdf(blob, paperId),
    onError: (error) => toast(`${tr('下载失败：', 'Couldn’t download: ')}${error instanceof Error ? error.message : String(error)}`, 'error'),
  });

  const assets = assetsQuery.data?.items ?? [];
  const preferred = assets.find((asset) => asset.is_preferred) ?? assets[0] ?? null;
  const version = versionQuery.data ?? null;
  const parseMeta = parseStateMeta(version?.status);
  const parsing = Boolean(version && ACTIVE_PARSE_STATES.has(version.status));
  const indexState = vectorStateMeta(combinedVectorState(version?.document_vector_state, version?.chunk_vector_state));
  const pageCount = version?.page_count;

  return (
    <section
      className="paper-asset-panel"
      aria-label={tr('PDF 与全文', 'PDF & full text')}
      style={{
        marginTop: 16,
        padding: '14px 0',
        borderTop: '0.5px solid var(--border)',
        borderBottom: '0.5px solid var(--border)',
      }}
    >
      <div className="row gap10 wrap paper-asset-summary" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 600 }}>{tr('PDF 与全文', 'PDF & full text')}</div>
          <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
            {preferred
              ? [
                  sourceLabel(preferred.source),
                  formatBytes(preferred.byte_size),
                  pageCount ? tr(`${pageCount} 页`, `${pageCount} ${pageCount === 1 ? 'page' : 'pages'}`) : null,
                ]
                  .filter(Boolean)
                  .join(' · ')
              : tr('还没有 PDF', 'No PDF yet')}
          </div>
        </div>
        <div className="row gap6 wrap paper-asset-states">
          <StateDot tone={parseMeta.tone} title={parseMeta.detail}>{parseMeta.label}</StateDot>
          <StateDot tone={indexState.tone} title={tr('用于检索和定位原句', 'Used for search and locating quoted sentences')}>
            {indexState.label}
          </StateDot>
        </div>
      </div>

      {version?.error_code && (
        <div className="mono" style={{ marginTop: 10, color: 'var(--danger-tx)', fontSize: 11 }}>
          {version.error_code}{version.error_detail ? ` · ${version.error_detail}` : ''}
        </div>
      )}

      <div className="row gap8 wrap paper-asset-actions" style={{ marginTop: 12 }}>
        {preferred && (
          <button className="btn btn-ghost sm" disabled={downloadMutation.isPending} onClick={() => downloadMutation.mutate(preferred)}>
            <Icon name="download" size={13} />
            {downloadMutation.isPending ? tr('下载中…', 'Downloading…') : tr('下载 PDF', 'Download PDF')}
          </button>
        )}
        <>
          <input
            ref={inputRef}
            type="file"
            accept="application/pdf,.pdf"
            hidden
            onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              event.currentTarget.value = '';
              if (file) uploadMutation.mutate(file);
            }}
          />
          <button className="btn btn-soft sm" disabled={uploadMutation.isPending} onClick={() => inputRef.current?.click()}>
            <Icon name={uploadMutation.isPending ? 'refresh' : 'plus'} size={13} style={uploadMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
            {uploadMutation.isPending ? tr('上传中…', 'Uploading…') : tr('上传 PDF', 'Upload PDF')}
          </button>
          {preferred && (
            <button className="btn btn-ghost sm" disabled={parseMutation.isPending || parsing} onClick={() => setReparseAsset(preferred)}>
              <Icon name="refresh" size={13} />
              {version?.status === 'failed' ? tr('重试解析', 'Retry parsing') : tr('重新解析', 'Reparse')}
            </button>
          )}
        </>
      </div>


      <ConfirmModal
        open={reparseAsset !== null}
        onClose={() => setReparseAsset(null)}
        title={tr('重新解析 PDF？', 'Reparse the PDF?')}
        message={tr(
          '全文和索引会重新生成，已有引用可能无法再定位到原句。PDF 文件保留。',
          'The full text and index are rebuilt, so existing citations may no longer point to exact sentences. The PDF is kept.',
        )}
        confirmText={tr('重新解析', 'Reparse')}
        busy={parseMutation.isPending}
        onConfirm={() => {
          if (reparseAsset) parseMutation.mutate(reparseAsset.id);
        }}
      />
    </section>
  );
}
