import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Markdown } from '../markdown';

describe('Markdown standard images', () => {
  it('renders authorized structured-content images lazily', () => {
    const html = renderToStaticMarkup(
      <Markdown source={'![Figure 1](/api/structured-content-assets/signed-token)'} />,
    );

    expect(html).toContain('<img');
    expect(html).toContain('loading="lazy"');
    expect(html).toContain('/api/structured-content-assets/signed-token');
    expect(html).toContain('Figure 1');
  });

  it('does not render script URLs as images', () => {
    const html = renderToStaticMarkup(<Markdown source={'![bad](javascript:alert(1))'} />);
    expect(html).not.toContain('<img');
    expect(html).not.toContain('src="javascript:');
  });

  it('does not render active SVG data URLs', () => {
    const html = renderToStaticMarkup(
      <Markdown source={'![bad](data:image/svg+xml;base64,PHN2Zy8+)'} />,
    );
    expect(html).not.toContain('<img');
  });

  it('does not render protocol-relative images', () => {
    // `//evil.com/x.png` 以 `/` 开头，会被「根相对路径」那一条放行。
    // 不是 XSS，但 Wiki 是全局共享的，等于给任意站点一个追踪像素。
    const html = renderToStaticMarkup(<Markdown source={'![bad](//evil.com/x.png)'} />);
    expect(html).not.toContain('<img');
    expect(html).not.toContain('evil.com');
  });
});
