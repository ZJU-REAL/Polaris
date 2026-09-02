import type { CitationRenderer } from '../../lib/markdown';
import { tr } from '../../lib/i18n';
import './evidence.css';

const EVIDENCE_MANIFEST_RE = /\n?<!-- polaris-ai-evidence:(\{.*?\}) -->\s*$/s;

export interface AIEvidenceReference {
  article_no: number;
  sentence_no: number;
  paper_id?: string | null;
  content_version_id?: string | null;
  anchor_id?: string | null;
  source?: string | null;
}

export interface ParsedEvidenceArtifact {
  body: string;
  refs: AIEvidenceReference[];
}

export function parseEvidenceArtifact(source: string): ParsedEvidenceArtifact {
  const match = EVIDENCE_MANIFEST_RE.exec(source);
  if (!match) return { body: source, refs: [] };
  try {
    const payload = JSON.parse(match[1]!) as { refs?: unknown };
    const refs = Array.isArray(payload.refs)
      ? payload.refs.filter((item): item is AIEvidenceReference => {
          if (!item || typeof item !== 'object') return false;
          const ref = item as Partial<AIEvidenceReference>;
          return Number.isInteger(ref.article_no) && Number.isInteger(ref.sentence_no);
        })
      : [];
    return { body: source.slice(0, match.index).trimEnd(), refs };
  } catch {
    return { body: source.slice(0, match.index).trimEnd(), refs: [] };
  }
}

function matchingReference(
  refs: AIEvidenceReference[],
  articleNo: number,
  sentenceNo?: number,
): AIEvidenceReference | null {
  if (sentenceNo === undefined) return null;
  const matches = refs.filter(
    (ref) => ref.article_no === articleNo && ref.sentence_no === sentenceNo,
  );
  return matches.length === 1 ? matches[0]! : null;
}

export function evidenceCitationRenderer({
  libraryId,
  fallbackPaperId,
  title,
  refs,
}: {
  libraryId?: string | null;
  fallbackPaperId: string;
  title: string;
  refs: AIEvidenceReference[];
}): CitationRenderer {
  return (articleNo, sentenceNo, label) => {
    const ref = matchingReference(refs, articleNo, sentenceNo);
    const paperId = ref?.paper_id || fallbackPaperId;
    const params = new URLSearchParams();
    if (libraryId && ref?.anchor_id) {
      params.set('library_id', libraryId);
      params.set('evidence', ref.anchor_id);
      if (ref.content_version_id) params.set('content_version_id', ref.content_version_id);
    }
    const href = `/papers/${encodeURIComponent(paperId)}/read${params.size ? `?${params}` : ''}`;
    const exact = Boolean(libraryId && ref?.anchor_id);
    return (
      <a
        href={href}
        className="evidence-citation"
        title={exact
          ? `${title} · ${tr('定位原文句子', 'Locate source sentence')}`
          : `${title} · ${tr('打开论文来源', 'Open paper source')}`}
      >
        {label || (sentenceNo ? `[文${articleNo}·句${sentenceNo}]` : `[文${articleNo}]`)}
      </a>
    );
  };
}
