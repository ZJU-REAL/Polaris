import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Markdown } from '../../../lib/markdown';
import { evidenceCitationRenderer, parseEvidenceArtifact } from '../evidenceArtifact';

describe('AI evidence artifact citations', () => {
  it('removes the private manifest and resolves a unique sentence anchor', () => {
    const source = 'Grounded claim [文1·句2].\n\n<!-- polaris-ai-evidence:{"refs":[{"article_no":1,"sentence_no":2,"paper_id":"paper-a","content_version_id":"version-a","anchor_id":"anchor-a"}]} -->';
    const artifact = parseEvidenceArtifact(source);
    const html = renderToStaticMarkup(
      <Markdown
        source={artifact.body}
        renderCitation={evidenceCitationRenderer({
          libraryId: 'library-a',
          fallbackPaperId: 'paper-fallback',
          title: 'Evidence paper',
          refs: artifact.refs,
        })}
      />,
    );

    expect(artifact.body).not.toContain('polaris-ai-evidence');
    expect(html).toContain('/papers/paper-a/read?library_id=library-a&amp;evidence=anchor-a&amp;content_version_id=version-a');
    expect(html).toContain('[文1·句2]');
  });

  it('falls back to the paper when the sentence reference is ambiguous', () => {
    const renderCitation = evidenceCitationRenderer({
      libraryId: 'library-a',
      fallbackPaperId: 'paper-a',
      title: 'Evidence paper',
      refs: [
        { article_no: 1, sentence_no: 1, paper_id: 'paper-a', anchor_id: 'first' },
        { article_no: 1, sentence_no: 1, paper_id: 'paper-a', anchor_id: 'second' },
      ],
    });
    const html = renderToStaticMarkup(<Markdown source="[文1·段3·句1]" renderCitation={renderCitation} />);

    expect(html).toContain('href="/papers/paper-a/read"');
    expect(html).not.toContain('evidence=');
  });
});
