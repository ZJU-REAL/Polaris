/**
 * 收录设置不再把 arXiv 与 cs.* 当成人人适用的默认值（#821）。
 *
 * 以前没选来源时一律显示并使用 arXiv，arXiv 分类块于是总在，且摆着一排写死的
 * cs.CL / cs.AI / cs.LG……——一个做临床的人建库时第一眼看到的是计算机的分类。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { InclusionSettingsForm, type InclusionValue } from '../InclusionSettingsForm';

const SOURCES = [
  { id: 'openalex', title: 'OpenAlex', description: '' },
  { id: 'arxiv', title: 'arXiv', description: '' },
  { id: 'pubmed', title: 'PubMed', description: '' },
];

const EMPTY: InclusionValue = {
  sources: [],
  arxiv_categories: [],
  include: [],
  exclude: [],
  rubric: [],
  anchors: [],
};

function render(props: { defaultSources: string[]; arxivQuickPicks?: string[]; value?: Partial<InclusionValue> }) {
  const client = new QueryClient();
  client.setQueryData(['literature-sources'], SOURCES);
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <InclusionSettingsForm
        value={{ ...EMPTY, ...props.value }}
        onChange={() => {}}
        defaultSources={props.defaultSources}
        arxivQuickPicks={props.arxivQuickPicks}
      />
    </QueryClientProvider>,
  );
}

/** 渲染结果里被点亮的来源 chip 的文字。 */
function selectedSources(html: string): string[] {
  return [...html.matchAll(/class="chip on"[^>]*>([^<]+)</g)].map((m) => m[1] ?? '');
}

describe('inclusion form defaults', () => {
  it('shows the defaults it is given when no source is picked', () => {
    const html = render({ defaultSources: ['pubmed'] });
    expect(selectedSources(html)).toEqual(['PubMed']);
  });

  it('hides arXiv categories unless arXiv is actually in use', () => {
    expect(render({ defaultSources: ['pubmed'] })).not.toContain('arXiv 分类');
    expect(render({ defaultSources: ['openalex'] })).not.toContain('arXiv 分类');
    expect(render({ defaultSources: ['arxiv', 'openalex'] })).toContain('arXiv 分类');
  });

  it('offers no computer-science categories unless the discipline supplies them', () => {
    const html = render({ defaultSources: ['arxiv'] });
    expect(html).toContain('arXiv 分类');
    for (const cat of ['cs.CL', 'cs.AI', 'cs.LG', 'cs.CV']) expect(html).not.toContain(cat);
  });

  it('offers the discipline’s own quick picks', () => {
    const html = render({ defaultSources: ['arxiv'], arxivQuickPicks: ['q-bio.GN', 'q-bio.NC'] });
    expect(html).toContain('q-bio.GN');
    expect(html).toContain('q-bio.NC');
    expect(html).not.toContain('cs.CL');
  });

  it('an explicit choice wins over the defaults', () => {
    const html = render({ defaultSources: ['arxiv'], value: { sources: ['pubmed', 'openalex'] } });
    expect(selectedSources(html).sort()).toEqual(['OpenAlex', 'PubMed']);
    expect(html).not.toContain('arXiv 分类');
  });
});
