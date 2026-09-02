import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ExtensionApiKeySettings } from '../ExtensionApiKeySettings';

function renderSettings(): string {
  const client = new QueryClient();
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <ExtensionApiKeySettings />
    </QueryClientProvider>,
  );
}

describe('Polaris extension API key settings', () => {
  it('renders the connection URL and one-time key lifecycle controls', () => {
    const html = renderSettings();
    expect(html).toContain('Polaris 扩展');
    expect(html).toContain('Polaris 地址');
    expect(html).toContain('生成 / 轮换 API Key');
    expect(html).toContain('撤销 API Key');
    expect(html).toContain('不会显示现有 Key 的状态');
  });

  it('does not render any plaintext key before the create response', () => {
    const html = renderSettings();
    expect(html).not.toContain('pol_dl_');
    expect(html).not.toContain('API Key（仅本次显示）');
    expect(html).not.toContain('测试连接');
  });
});
