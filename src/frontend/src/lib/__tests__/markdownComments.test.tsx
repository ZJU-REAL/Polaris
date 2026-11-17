import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Markdown } from '../markdown';

/* HTML 注释不是给人看的：渲染器把原文里的 HTML 当文字显示，注释也就成了一段「乱码」
   （#873：解读末尾的 <!-- polaris-ai-evidence:{…} -->）。注释一律不显示；代码里的照留。 */
describe('Markdown HTML comments', () => {
  it('drops a trailing machine comment', () => {
    const html = renderToStaticMarkup(
      <Markdown
        source={
          '## 结果\n\n有依据的结论。\n\n<!-- polaris-ai-evidence:{"version":1,"refs":[{"article_no":1,"sentence_no":31,"anchor_id":"a-1","parser":"pymupdf"}]} -->\n'
        }
      />,
    );

    expect(html).toContain('有依据的结论。');
    expect(html).not.toContain('polaris-ai-evidence');
    expect(html).not.toContain('anchor_id');
    expect(html).not.toContain('&lt;!--');
    expect(html).not.toContain('--&gt;');
  });

  it('drops inline and multi-line comments, keeping the text around them', () => {
    const html = renderToStaticMarkup(
      <Markdown source={'前半句<!-- 行内 -->后半句\n\n<!--\n多行\n注释\n-->\n\n下一段'} />,
    );

    expect(html).toContain('前半句后半句');
    expect(html).toContain('下一段');
    expect(html).not.toContain('行内');
    expect(html).not.toContain('多行');
    expect(html).not.toContain('&lt;!--');
  });

  it('keeps comments that are code', () => {
    const html = renderToStaticMarkup(
      <Markdown source={'用 `<!-- note -->` 写注释：\n\n```html\n<!-- in a block -->\n<p>hi</p>\n```\n'} />,
    );

    expect(html).toContain('&lt;!-- note --&gt;');
    expect(html).toContain('&lt;!-- in a block --&gt;');
  });

  it('shows an unterminated comment opener as text instead of swallowing the rest', () => {
    const html = renderToStaticMarkup(<Markdown source={'a <!-- 没有结尾\n\n后面的正文'} />);

    expect(html).toContain('后面的正文');
  });
});
