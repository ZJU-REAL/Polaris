import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type PaperDetail } from '../../lib/api';
import { tr } from '../../lib/i18n';

function uploadError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.message.includes('PDF_UPLOAD_TOO_LARGE')) {
      return tr('PDF 不能超过 100 MB', 'The PDF must be 100 MB or smaller');
    }
    if (error.message.includes('PDF_UPLOAD_INVALID')) {
      return tr('不是有效的 PDF，或文件已加密', 'This isn’t a valid PDF, or it’s password-protected');
    }
    if (error.message.includes('PDF_UPLOAD_EMPTY')) {
      return tr('文件是空的', 'The file is empty');
    }
    if (error.message.includes('PDF_ALREADY_EXISTS')) {
      return tr('这篇论文已经有 PDF', 'This paper already has a PDF');
    }
    // 后端把不可用的原因拼在错误码后面（粘成落地页、站点要登录、指向内网……）。
    // 这类失败几乎都是用户自己能修的，所以原样透出去，不要压成一句「失败」。
    const unusable = /PDF_URL_UNUSABLE:\s*(.+)$/.exec(error.message);
    if (unusable) return unusable[1]!.trim();
  }
  return error instanceof Error ? error.message : String(error);
}

export function PdfUploadButton({
  paperId,
  pdfAvailable,
}: {
  paperId: string;
  pdfAvailable: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [linkOpen, setLinkOpen] = useState(false);
  const [url, setUrl] = useState('');
  const queryClient = useQueryClient();
  const applyDetail = (detail: PaperDetail) => {
    queryClient.setQueriesData<PaperDetail>({ queryKey: ['paper'] }, (old) =>
      old?.id === paperId ? detail : old,
    );
    void queryClient.invalidateQueries({ queryKey: ['papers'] });
    void queryClient.invalidateQueries({ queryKey: ['library'] });
    void queryClient.invalidateQueries({ queryKey: ['shelf'] });
  };
  const mutation = useMutation({
    mutationFn: (file: File) => api.uploadPaperPdf(paperId, file),
    onSuccess: (detail) => {
      applyDetail(detail);
      toast(tr('PDF 已上传', 'PDF uploaded'), 'ok');
    },
    onError: (error) => toast(`${tr('上传失败：', 'Couldn’t upload: ')}${uploadError(error)}`, 'error'),
  });

  const urlMutation = useMutation({
    mutationFn: (url: string) => api.uploadPaperPdfFromUrl(paperId, url),
    onSuccess: (detail) => {
      applyDetail(detail);
      setUrl('');
      setLinkOpen(false);
      toast(tr('PDF 已下载', 'PDF downloaded'), 'ok');
    },
    onError: (error) => toast(`${tr('下载失败：', 'Couldn’t download: ')}${uploadError(error)}`, 'error'),
  });

  if (pdfAvailable) return null;

  const busy = mutation.isPending || urlMutation.isPending;

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        hidden
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = '';
          if (file) mutation.mutate(file);
        }}
      />
      <button
        type="button"
        className="btn btn-ghost sm"
        aria-label={tr('上传 PDF', 'Upload PDF')}
        aria-busy={mutation.isPending}
        disabled={busy}
        onClick={() => inputRef.current?.click()}
      >
        <Icon
          name={mutation.isPending ? 'refresh' : 'download'}
          size={13}
          style={mutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined}
        />
        {mutation.isPending ? tr('上传中…', 'Uploading…') : tr('上传 PDF', 'Upload PDF')}
      </button>
      <button
        type="button"
        className="btn btn-ghost sm"
        aria-label={tr('用链接添加 PDF', 'Add PDF from a link')}
        disabled={busy}
        onClick={() => setLinkOpen((open) => !open)}
      >
        <Icon name="link" size={13} />
        {tr('从链接添加', 'Add from link')}
      </button>
      {linkOpen && (
        <form
          className="row gap-xs"
          onSubmit={(event) => {
            event.preventDefault();
            const trimmed = url.trim();
            if (trimmed) urlMutation.mutate(trimmed);
          }}
        >
          <input
            className="input sm"
            type="url"
            value={url}
            autoFocus
            placeholder={tr('例如 https://arxiv.org/pdf/2005.11401', 'e.g. https://arxiv.org/pdf/2005.11401')}
            onChange={(event) => setUrl(event.target.value)}
          />
          <button type="submit" className="btn sm" disabled={busy || !url.trim()}>
            {urlMutation.isPending ? tr('下载中…', 'Downloading…') : tr('下载', 'Download')}
          </button>
        </form>
      )}
    </>
  );
}
