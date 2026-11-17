import type { CitationRenderer } from '../../lib/markdown';
import { tr } from '../../lib/i18n';
import './evidence.css';

export interface AIEvidenceReference {
  article_no: number;
  sentence_no: number;
  paper_id?: string | null;
  content_version_id?: string | null;
  anchor_id?: string | null;
  source?: string | null;
}

/**
 * 解读正文里 [文N·句M] 引用的定位信息（论文详情的 wiki_evidence）。
 * 引擎把它和正文分开给：正文只放给人看的内容（#873）。
 */
export interface WikiEvidence {
  version?: number;
  mode?: string;
  paper_count?: number;
  refs?: unknown;
}

/** 从 wiki_evidence 里取出可用的引用定位；没有或形状不对的一律当作没有。 */
export function evidenceRefs(evidence: WikiEvidence | null | undefined): AIEvidenceReference[] {
  const refs = evidence?.refs;
  if (!Array.isArray(refs)) return [];
  return refs.filter((item): item is AIEvidenceReference => {
    if (!item || typeof item !== 'object') return false;
    const ref = item as Partial<AIEvidenceReference>;
    return Number.isInteger(ref.article_no) && Number.isInteger(ref.sentence_no);
  });
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
          ? `${title} · ${tr('跳到原句', 'Go to the quoted sentence')}`
          : `${title} · ${tr('打开论文', 'Open paper')}`}
      >
        {label || (sentenceNo ? `[文${articleNo}·句${sentenceNo}]` : `[文${articleNo}]`)}
      </a>
    );
  };
}
