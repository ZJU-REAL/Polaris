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
}

export function parseStateMeta(status?: string | null): AssetStateMeta {
  switch (status) {
    case 'queued':
      return { label: tr('等待解析', 'Queued'), tone: 'warning' };
    case 'mineru_uploading':
      return { label: tr('正在上传 MinerU', 'Uploading to MinerU'), tone: 'accent' };
    case 'mineru_acceptance_wait':
      return { label: tr('等待 MinerU 接收', 'Waiting for MinerU'), tone: 'accent' };
    case 'mineru_processing':
      return { label: tr('MinerU 解析中', 'MinerU parsing'), tone: 'accent' };
    case 'mineru_downloading':
      return { label: tr('正在获取 MinerU 结果', 'Fetching MinerU result'), tone: 'accent' };
    case 'parsing':
      return { label: tr('原文解析中', 'Parsing full text'), tone: 'accent' };
    case 'fallback_parsing':
      return { label: tr('PyMuPDF 兜底解析中', 'PyMuPDF fallback parsing'), tone: 'warning' };
    case 'ready':
      return { label: tr('结构化原文已就绪', 'Structured text ready'), tone: 'success' };
    case 'ready_fallback':
      return { label: tr('纯原文已就绪', 'Plain full text ready'), tone: 'success' };
    case 'vector_ready':
      return { label: tr('解析与向量化完成', 'Parsed and vectorized'), tone: 'success' };
    case 'failed':
      return { label: tr('解析失败', 'Parsing failed'), tone: 'danger' };
    default:
      return { label: tr('尚未解析', 'Not parsed'), tone: 'neutral' };
  }
}

export function vectorStateMeta(state?: string | null): AssetStateMeta {
  switch (state) {
    case 'ready':
      return { label: tr('已完成', 'Ready'), tone: 'success' };
    case 'building':
      return { label: tr('构建中', 'Building'), tone: 'accent' };
    case 'pending':
      return { label: tr('等待构建', 'Pending'), tone: 'warning' };
    case 'failed':
      return { label: tr('构建失败', 'Failed'), tone: 'danger' };
    default:
      return { label: tr('未建立', 'Not built'), tone: 'neutral' };
  }
}

function toneStyle(tone: AssetStateMeta['tone']): React.CSSProperties {
  if (tone === 'success') return { background: 'var(--ok-bg)', color: 'var(--ok-tx)' };
  if (tone === 'danger') return { background: 'var(--danger-bg)', color: 'var(--danger-tx)' };
  if (tone === 'warning') return { background: 'var(--warn-bg)', color: 'var(--warn-tx)' };
  if (tone === 'accent') return { background: 'var(--accent-soft)', color: 'var(--accent-text)' };
  return { background: 'var(--surface-3)', color: 'var(--text-3)' };
}

function formatBytes(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(value >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
}

function sourceLabel(source: string): string {
  const labels: Record<string, string> = {
    oa: 'OA',
    upload: tr('用户上传', 'Upload'),
    extension: tr('扩展归档', 'Extension'),
    arxiv: 'arXiv',
    manual: tr('手动导入', 'Manual'),
  };
  return labels[source] ?? source;
}

function sharingLabel(scope: string): string {
  if (scope === 'public') return tr('公开复用', 'Public reuse');
  if (scope === 'library') return tr('本库共享', 'Library shared');
  return tr('私有授权', 'Private grant');
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
  canManage,
}: {
  libraryId: string;
  paperId: string;
  doi?: string | null;
  canManage: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const [sharingScope, setSharingScope] = useState<'private' | 'library' | 'public'>('library');
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
      toast(tr('PDF 已保存，MinerU 优先解析任务已开始', 'PDF saved; MinerU-first parsing has started'), 'ok');
    },
    onError: (error) => {
      invalidate();
      toast(`${tr('PDF 处理失败：', 'PDF processing failed: ')}${error instanceof Error ? error.message : String(error)}`, 'error');
    },
  });

  const parseMutation = useMutation({
    mutationFn: (assetId: string) => api.createLibraryPaperContentVersion(libraryId, paperId, assetId),
    onSuccess: () => {
      setReparseAsset(null);
      invalidate();
      toast(tr('已提交新的解析版本', 'A new parse version was queued'), 'ok');
    },
    onError: (error) => toast(`${tr('提交解析失败：', 'Failed to queue parsing: ')}${error instanceof Error ? error.message : String(error)}`, 'error'),
  });

  const downloadMutation = useMutation({
    mutationFn: (asset: PaperAssetRead) => api.downloadLibraryPaperAsset(libraryId, paperId, asset.id),
    onSuccess: (blob) => savePdf(blob, paperId),
    onError: (error) => toast(`${tr('下载失败：', 'Download failed: ')}${error instanceof Error ? error.message : String(error)}`, 'error'),
  });

  const assets = assetsQuery.data?.items ?? [];
  const preferred = assets.find((asset) => asset.is_preferred) ?? assets[0] ?? null;
  const version = versionQuery.data ?? null;
  const parseMeta = parseStateMeta(version?.status);
  const parsing = Boolean(version && ACTIVE_PARSE_STATES.has(version.status));
  const documentVector = vectorStateMeta(version?.document_vector_state);
  const chunkVector = vectorStateMeta(version?.chunk_vector_state);

  return (
    <section
      className="paper-asset-panel"
      aria-label={tr('PDF 资产与全文索引', 'PDF asset and full-text index')}
      style={{
        marginTop: 16,
        padding: '14px 0',
        borderTop: '0.5px solid var(--border)',
        borderBottom: '0.5px solid var(--border)',
      }}
    >
      <div className="row gap10 wrap paper-asset-summary" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: 12.5, fontWeight: 650 }}>{tr('PDF 资产与全文索引', 'PDF asset and full-text index')}</div>
          <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>
            {preferred
              ? `${sourceLabel(preferred.source)} · ${sharingLabel(preferred.sharing_scope)} · ${formatBytes(preferred.byte_size)}`
              : tr('尚未绑定可处理的 PDF', 'No processable PDF is attached')}
          </div>
        </div>
        <div className="row gap6 wrap paper-asset-states">
          <span className="pill sm" style={toneStyle(parseMeta.tone)}>{parseMeta.label}</span>
          <span className="pill sm" style={toneStyle(documentVector.tone)} title={tr('标题、摘要与全文摘要的论文级检索向量', 'Paper-level retrieval vector')}>
            {tr('论文向量', 'Paper vector')} · {documentVector.label}
          </span>
          <span className="pill sm" style={toneStyle(chunkVector.tone)} title={tr('供句子级证据检索使用的全文分块向量', 'Full-text chunk vectors for sentence-level evidence retrieval')}>
            {tr('全文分块', 'Full-text chunks')} · {chunkVector.label}
          </span>
        </div>
      </div>

      {version?.error_code && (
        <div style={{ marginTop: 10, color: 'var(--danger-tx)', fontSize: 11.5 }}>
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
        {canManage && (
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
            <select
              className="input sm"
              aria-label={tr('PDF 共享范围', 'PDF sharing scope')}
              value={sharingScope}
              disabled={uploadMutation.isPending}
              onChange={(event) => setSharingScope(event.target.value as typeof sharingScope)}
            >
              <option value="private">{tr('私有授权', 'Private grant')}</option>
              <option value="library">{tr('本库共享', 'Library shared')}</option>
              <option value="public">{tr('公开复用', 'Public reuse')}</option>
            </select>
            <button className="btn btn-soft sm" disabled={uploadMutation.isPending} onClick={() => inputRef.current?.click()}>
              <Icon name={uploadMutation.isPending ? 'refresh' : 'plus'} size={13} style={uploadMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
              {uploadMutation.isPending ? tr('上传并排队…', 'Uploading and queuing…') : tr('上传 PDF', 'Upload PDF')}
            </button>
            {preferred && (
              <button className="btn btn-ghost sm" disabled={parseMutation.isPending || parsing} onClick={() => setReparseAsset(preferred)}>
                <Icon name="refresh" size={13} />
                {version?.status === 'failed' ? tr('重试解析', 'Retry parsing') : tr('重新解析', 'Reparse')}
              </button>
            )}
          </>
        )}
      </div>

      {version && (
        <div className="mono" style={{ marginTop: 10, fontSize: 10.5, color: 'var(--text-4)' }}>
          {tr(`内容版本 v${version.version_no} · ${version.parser} · ${version.page_count} 页 · ${version.chunk_count} 个分块`, `Content v${version.version_no} · ${version.parser} · ${version.page_count} pages · ${version.chunk_count} chunks`)}
        </div>
      )}

      <ConfirmModal
        open={reparseAsset !== null}
        onClose={() => setReparseAsset(null)}
        title={tr('重新解析 PDF', 'Reparse PDF')}
        message={(
          <>
            <div style={{ marginBottom: 10 }}>
              {tr('将创建新的内容版本并重新构建全文索引。', 'A new content version and full-text index will be created.')}
            </div>
            <div style={{ padding: 12, borderRadius: 6, background: 'var(--warn-bg)', color: 'var(--warn-tx)', lineHeight: 1.65 }}>
              {tr(
                '重新解析会替换当前结构化原文、分块和向量。旧 AI 内容仍能打开论文，但原句定位失效时会退回论文级来源。源 PDF 不会删除。',
                'Reparsing replaces the current structured text, chunks, and vectors. Existing AI content still opens the paper, but stale sentence anchors fall back to the paper-level source. The source PDF is retained.',
              )}
            </div>
          </>
        )}
        confirmText={tr('确认重新解析', 'Reparse')}
        busy={parseMutation.isPending}
        onConfirm={() => {
          if (reparseAsset) parseMutation.mutate(reparseAsset.id);
        }}
      />
    </section>
  );
}
