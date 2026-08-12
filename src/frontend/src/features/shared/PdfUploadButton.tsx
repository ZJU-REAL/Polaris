import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type PaperDetail } from '../../lib/api';
import { tr } from '../../lib/i18n';

function uploadError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.message.includes('PDF_UPLOAD_TOO_LARGE')) {
      return tr('PDF 超过 100 MB 上限', 'The PDF exceeds the 100 MB limit');
    }
    if (error.message.includes('PDF_UPLOAD_INVALID')) {
      return tr('文件不是有效的 PDF，或 PDF 已加密', 'The file is not a valid PDF or is password-protected');
    }
    if (error.message.includes('PDF_UPLOAD_EMPTY')) {
      return tr('所选 PDF 是空文件', 'The selected PDF is empty');
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
      toast(tr('PDF 已上传，可以阅读全文了', 'PDF uploaded — the full paper is ready to read'), 'ok');
    },
    onError: (error) => toast(`${tr('上传 PDF 失败：', 'PDF upload failed: ')}${uploadError(error)}`, 'error'),
  });

  const urlMutation = useMutation({
    mutationFn: (url: string) => api.uploadPaperPdfFromUrl(paperId, url),
    onSuccess: (detail) => {
      applyDetail(detail);
      setUrl('');
      setLinkOpen(false);
      toast(tr('PDF 已取回，可以阅读全文了', 'PDF fetched — the full paper is ready to read'), 'ok');
    },
    onError: (error) => toast(`${tr('按链接取 PDF 失败：', 'Fetching the PDF failed: ')}${uploadError(error)}`, 'error'),
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
        {mutation.isPending ? tr('上传处理中…', 'Uploading…') : tr('上传 PDF', 'Upload PDF')}
      </button>
      <button
        type="button"
        className="btn btn-ghost sm"
        aria-label={tr('用链接添加 PDF', 'Add PDF from a link')}
        disabled={busy}
        onClick={() => setLinkOpen((open) => !open)}
      >
        <Icon name="link" size={13} />
        {tr('用链接', 'From link')}
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
            placeholder={tr('可直接下载的 PDF 链接', 'Direct link to a PDF')}
            onChange={(event) => setUrl(event.target.value)}
          />
          <button type="submit" className="btn sm" disabled={busy || !url.trim()}>
            {urlMutation.isPending ? tr('取回中…', 'Fetching…') : tr('取回', 'Fetch')}
          </button>
        </form>
      )}
    </>
  );
}
