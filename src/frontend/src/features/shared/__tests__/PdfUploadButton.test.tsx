import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { PdfUploadButton } from '../PdfUploadButton';

function render(pdfAvailable: boolean): string {
  const client = new QueryClient();
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <PdfUploadButton paperId="paper-id" pdfAvailable={pdfAvailable} />
    </QueryClientProvider>,
  );
}

describe('PdfUploadButton', () => {
  it('没有 PDF 时显示上传入口和 PDF 文件选择器', () => {
    const html = render(false);
    expect(html).toContain('type="file"');
    expect(html).toContain('accept="application/pdf,.pdf"');
    expect(html).toContain('上传 PDF');
  });

  it('已经有 PDF 时不渲染任何上传入口', () => {
    expect(render(true)).toBe('');
  });
});
