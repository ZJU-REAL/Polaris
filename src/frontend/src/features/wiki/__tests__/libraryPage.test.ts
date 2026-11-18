import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { Markdown } from '../../../lib/markdown';
import { findQuotePage } from '../../reading/evidenceText';
import { readerHref } from '../../reading/shared';
import { LEGACY_TABS, normalizeLibraryParams, readLibraryParams } from '../libraryTabs';
import { fulltextState } from '../../shared/PaperFulltext';
import { splitSummary } from '../../shared/PaperDetail';
import { normalizeEvidenceMarkers } from '../../../lib/markdown';
import { plainSummary } from '../../../lib/format';

const norm = (qs: string) => normalizeLibraryParams(new URLSearchParams(qs))?.toString() ?? null;

describe('library page addresses', () => {
  it('maps every one of the twelve old tabs somewhere', () => {
    const old = ['discover', 'papers', 'concepts', 'methods', 'graph', 'gaps', 'digest', 'chat', 'qa', 'notes', 'govern', 'ingest'];
    for (const t of old) {
      const next = norm(`tab=${t}`);
      // papers is the default: the param just goes away
      expect(next, t).not.toBeNull();
    }
    expect(Object.keys(LEGACY_TABS)).toEqual(expect.arrayContaining(old.filter((t) => t !== 'papers')));
  });

  it('sends the two find-papers tabs to one place', () => {
    expect(norm('tab=discover')).toBe('tab=find');
    expect(norm('tab=ingest')).toBe('tab=find');
  });

  it('puts the knowledge tabs under knowledge with their view', () => {
    expect(norm('tab=graph')).toBe('tab=knowledge&view=graph');
    expect(norm('tab=notes')).toBe('tab=knowledge&view=notes');
  });

  it('merges chat and deep Q&A into one ask tab with a mode', () => {
    expect(norm('tab=chat')).toBe('tab=ask&mode=quick');
    expect(norm('tab=qa')).toBe('tab=ask&mode=thorough');
  });

  it('opens settings as a drawer over the papers tab', () => {
    expect(norm('tab=govern')).toBe('settings=1');
  });

  it('keeps other params such as the selected paper', () => {
    expect(norm('tab=qa&paper=p1')).toBe('tab=ask&paper=p1&mode=thorough');
  });

  it('leaves new addresses alone and drops unknown tabs', () => {
    expect(norm('tab=find')).toBeNull();
    expect(norm('paper=p1')).toBeNull();
    expect(norm('tab=nonsense')).toBe('');
  });

  it('reads defaults for missing or unknown values', () => {
    const r = readLibraryParams(new URLSearchParams('tab=knowledge&view=bogus'));
    expect(r).toMatchObject({ tab: 'knowledge', view: 'concepts', mode: 'quick', settings: false, paper: null });
  });
});

describe('PDF and full text state', () => {
  it('trusts the old pipeline when the new one has no record', () => {
    // pdf_available + chunks from the PDF, but no asset / content version: must not say "no PDF"
    expect(fulltextState({ pdfAvailable: true, hasAsset: false, versionStatus: null, hasFulltext: true })).toBe('ready');
  });
  it('says PDF only when there is a PDF but no extracted text', () => {
    expect(fulltextState({ pdfAvailable: true, hasAsset: false, versionStatus: null, hasFulltext: false })).toBe('pdf_only');
  });
  it('shows extraction in progress and failures', () => {
    expect(fulltextState({ pdfAvailable: false, hasAsset: true, versionStatus: 'mineru_processing', hasFulltext: false })).toBe('extracting');
    expect(fulltextState({ pdfAvailable: false, hasAsset: true, versionStatus: 'failed', hasFulltext: false })).toBe('failed');
  });
  it('says abstract only when there is nothing', () => {
    expect(fulltextState({ pdfAvailable: false, hasAsset: false, versionStatus: undefined, hasFulltext: false })).toBe('none');
  });
});

describe('summary text', () => {
  it('takes the TL;DR section out of the body so it is shown once', () => {
    const { lead, body } = splitSummary('## TL;DR\n\nOne line.\n\n## 方法\n\nBody.');
    expect(lead).toBe('One line.');
    expect(body.startsWith('## 方法')).toBe(true);
  });
  it('leaves a summary without a TL;DR alone', () => {
    expect(splitSummary('## 方法\n\nBody.')).toEqual({ lead: '', body: '## 方法\n\nBody.' });
  });
  it('turns [EVIDENCE p=1·s=5] into the citation form the renderer knows', () => {
    expect(normalizeEvidenceMarkers('结果更好。[EVIDENCE p=1·s=5] 另外')).toBe('结果更好。[文1·句5] 另外');
    expect(normalizeEvidenceMarkers('no markers')).toBe('no markers');
  });
});

describe('plain one-line summaries', () => {
  it('drops markdown italics around journal names but keeps multiplication', () => {
    expect(plainSummary('这篇 2021 年发表在 *Nature Communications* 上的工作')).toBe('这篇 2021 年发表在 Nature Communications 上的工作');
    expect(plainSummary('a * b = c')).toBe('a * b = c');
  });
});

describe('reader passages', () => {
  it('keeps the second ordered item numbered 2 after a bullet list in between', () => {
    const html = renderToStaticMarkup(createElement(Markdown, { source: '1. VRAG\n- a\n- b\n2. JSA' }));
    expect(html).toContain('<ol start="2">');
  });
  it('finds the page a passage is on, despite hyphenation and ligatures', () => {
    const pages = [['Introduction text here'], ['We demonstrat-', 'ing the e\uFB03cacy of joint training on open domain QA']];
    expect(findQuotePage(pages, 'We demonstrating the efficacy of joint training')).toBe(2);
    expect(findQuotePage(pages, 'something that is not in the paper at all')).toBeNull();
  });
  it('links a passage to the reader with ?quote=, short ones without', () => {
    expect(readerHref('p1', 'short')).toBe('/papers/p1/read');
    expect(readerHref('p1', 'a  long   enough passage to look for')).toBe('/papers/p1/read?quote=a%20long%20enough%20passage%20to%20look%20for');
  });
});
