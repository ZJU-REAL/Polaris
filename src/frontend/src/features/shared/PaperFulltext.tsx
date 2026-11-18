import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { DotsMenu, type DotsMenuItem } from '../../components/ui/DotsMenu';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type PaperContentVersionRead } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { StateDot } from './StateDot';

/* ============================================================
   「PDF 与全文」一行：这篇论文现在能读到什么。

   引擎里有两套记录：老的（下载 arXiv PDF → 切段 → 建索引，看 pdf_available 和
   index-status）和新的（库里的 PDF 附件 + 解析版本）。只看其中一套会说错话——
   有 PDF 的论文显示「还没有 PDF」。这里两套都看，合成一句话：

     有 PDF 和全文 / 有 PDF，全文还没提取 / 正在提取全文 / 全文提取失败 / 只有摘要，没有 PDF
   ============================================================ */

const ACTIVE = new Set([
  'queued',
  'mineru_uploading',
  'mineru_acceptance_wait',
  'mineru_processing',
  'mineru_downloading',
  'parsing',
  'fallback_parsing',
]);

export type FulltextState = 'extracting' | 'failed' | 'ready' | 'pdf_only' | 'none';

/** 两套记录合成一个状态。导出给测试。 */
export function fulltextState(input: {
  pdfAvailable: boolean;
  hasAsset: boolean;
  versionStatus: string | null | undefined;
  hasFulltext: boolean | undefined;
}): FulltextState {
  const v = input.versionStatus;
  if (v && ACTIVE.has(v)) return 'extracting';
  if (v === 'failed' && !input.hasFulltext) return 'failed';
  if (input.hasFulltext || v === 'ready' || v === 'ready_fallback' || v === 'vector_ready') return 'ready';
  if (input.pdfAvailable || input.hasAsset) return 'pdf_only';
  return 'none';
}

export function fulltextLabel(s: FulltextState): string {
  switch (s) {
    case 'extracting':
      return tr('正在提取全文', 'Extracting the full text');
    case 'failed':
      return tr('全文提取失败', 'Couldn’t extract the full text');
    case 'ready':
      return tr('有 PDF 和全文', 'PDF and full text');
    case 'pdf_only':
      return tr('有 PDF，全文还没提取', 'PDF only, full text not extracted yet');
    default:
      return tr('只有摘要，没有 PDF', 'Abstract only, no PDF');
  }
}

const TONE: Record<FulltextState, 'success' | 'accent' | 'danger' | 'warning' | 'neutral'> = {
  ready: 'success',
  extracting: 'accent',
  failed: 'danger',
  pdf_only: 'warning',
  none: 'neutral',
};

export function PaperFulltextFact({
  libraryId,
  paperId,
  pdfAvailable,
  doi,
  onRead,
}: {
  /** 有库时才查库里的 PDF 附件、可上传 / 重新解析 */
  libraryId?: string;
  paperId: string;
  pdfAvailable: boolean;
  doi?: string | null;
  /** 有 PDF 时「打开 PDF」去阅读页 */
  onRead?: () => void;
}) {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [reparseOpen, setReparseOpen] = useState(false);

  const assetsQuery = useQuery({
    queryKey: ['paper-assets', libraryId, paperId],
    queryFn: () => api.listLibraryPaperAssets(libraryId!, paperId),
    enabled: !!libraryId,
    retry: false,
  });
  const versionQuery = useQuery({
    queryKey: ['paper-content-version', libraryId, paperId],
    queryFn: async () => {
      try {
        return await api.getLibraryPaperContentVersion(libraryId!, paperId);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    enabled: !!libraryId,
    retry: false,
    refetchInterval: (q) => {
      const v = q.state.data as PaperContentVersionRead | null | undefined;
      return v && ACTIVE.has(v.status) ? 2_500 : false;
    },
  });
  const indexQuery = useQuery({
    queryKey: ['paper-index-status', paperId],
    queryFn: () => api.getPaperIndexStatus(paperId),
    retry: false,
  });

  const invalidate = () => {
    for (const key of [
      ['paper-assets', libraryId, paperId],
      ['paper-content-version', libraryId, paperId],
      ['paper-index-status', paperId],
      ['paper', libraryId, paperId],
      ['papers', libraryId],
    ]) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  };

  const assets = assetsQuery.data?.items ?? [];
  const preferred = assets.find((a) => a.is_preferred) ?? assets[0] ?? null;
  const version = versionQuery.data ?? null;
  const state = fulltextState({
    pdfAvailable,
    hasAsset: !!preferred,
    versionStatus: version?.status,
    hasFulltext: indexQuery.data?.has_fulltext,
  });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      if (libraryId) {
        const asset = await api.uploadLibraryPaperAsset(libraryId, paperId, file, {
          sharingScope: 'library',
          identityKey: doi ? `doi:${doi.toLowerCase()}` : null,
        });
        await api.createLibraryPaperContentVersion(libraryId, paperId, asset.id);
      } else {
        await api.uploadPaperPdf(paperId, file);
      }
    },
    onSuccess: () => {
      invalidate();
      toast(tr('PDF 已上传，正在提取全文', 'PDF uploaded. Extracting the text.'), 'ok');
    },
    onError: (e) => toast(`${tr('PDF 上传失败：', 'Couldn’t upload the PDF: ')}${errorText(e)}`, 'error'),
  });
  const reparse = useMutation({
    mutationFn: (assetId: string) => api.createLibraryPaperContentVersion(libraryId!, paperId, assetId),
    onSuccess: () => {
      setReparseOpen(false);
      invalidate();
      toast(tr('已开始重新提取全文', 'Extracting the text again'), 'ok');
    },
    onError: (e) => toast(`${tr('无法开始提取：', 'Couldn’t start: ')}${errorText(e)}`, 'error'),
  });
  const download = useMutation({
    mutationFn: (assetId: string) => api.downloadLibraryPaperAsset(libraryId!, paperId, assetId),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${paperId}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    },
    onError: (e) => toast(`${tr('下载失败：', 'Couldn’t download: ')}${errorText(e)}`, 'error'),
  });

  const hasPdf = state !== 'none';
  const pages = version?.page_count;
  const detail = [
    state === 'ready' && pages ? tr(`${pages} 页`, `${pages} ${pages === 1 ? 'page' : 'pages'}`) : null,
    version?.error_code ? version.error_code : null,
  ]
    .filter(Boolean)
    .join(' · ');

  const menu: DotsMenuItem[] = [
    ...(preferred
      ? [{ key: 'dl', label: tr('下载 PDF', 'Download PDF'), icon: 'download' as const, onSelect: () => download.mutate(preferred.id) }]
      : []),
    { key: 'up', label: tr('换一份 PDF', 'Replace the PDF'), icon: 'plus' as const, onSelect: () => inputRef.current?.click() },
    ...(preferred
      ? [{ key: 're', label: tr('重新提取全文', 'Extract the text again'), icon: 'refresh' as const, onSelect: () => setReparseOpen(true) }]
      : []),
  ];

  return (
    <span className="pd-fulltext" data-testid="paper-fulltext" data-state={state}>
      <StateDot tone={TONE[state]}>{fulltextLabel(state)}</StateDot>
      {detail && <span className="pd-fact-note">{detail}</span>}
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        hidden
        onChange={(e) => {
          const file = e.currentTarget.files?.[0];
          e.currentTarget.value = '';
          if (file) upload.mutate(file);
        }}
      />
      {hasPdf && onRead && (
        <button type="button" className="btn btn-plain xs" onClick={onRead}>
          {tr('打开 PDF', 'Open PDF')}
        </button>
      )}
      {!hasPdf || state === 'failed' ? (
        <button type="button" className="btn btn-plain xs" disabled={upload.isPending} onClick={() => inputRef.current?.click()}>
          <Icon name={upload.isPending ? 'refresh' : 'plus'} size={12} style={upload.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
          {upload.isPending ? tr('上传中…', 'Uploading…') : tr('上传 PDF', 'Upload PDF')}
        </button>
      ) : (
        <DotsMenu plain items={menu} label={tr('PDF 的更多操作', 'More PDF actions')} />
      )}
      <ConfirmModal
        open={reparseOpen}
        onClose={() => setReparseOpen(false)}
        title={tr('重新提取全文？', 'Extract the text again?')}
        message={tr(
          '全文会重新生成，已有引用可能无法再定位到原句。PDF 文件保留。',
          'The text is rebuilt, so existing citations may no longer point to exact sentences. The PDF is kept.',
        )}
        confirmText={tr('重新提取', 'Extract again')}
        busy={reparse.isPending}
        onConfirm={() => {
          if (preferred) reparse.mutate(preferred.id);
        }}
      />
    </span>
  );
}
