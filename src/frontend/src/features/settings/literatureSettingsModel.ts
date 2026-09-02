import type {
  LiteratureSearchSettings,
  LiteratureSearchSettingsUpdate,
} from '../../lib/api';

export type CredentialMode = 'none' | 'optional' | 'required';

export interface SourceDefinition {
  id: string;
  zh: string;
  en: string;
  descriptionZh: string;
  descriptionEn: string;
  credentialMode: CredentialMode;
  metricOnly?: boolean;
  testQuery: string;
}

export const LITERATURE_SOURCES: SourceDefinition[] = [
  { id: 'openalex', zh: 'OpenAlex', en: 'OpenAlex', descriptionZh: '覆盖广、引用关系完整，支持多密钥轮询。', descriptionEn: 'Broad coverage and citation graph; supports rotating multiple keys.', credentialMode: 'optional', testQuery: 'structural engineering' },
  { id: 'semantic', zh: 'Semantic Scholar', en: 'Semantic Scholar', descriptionZh: '补充摘要、引用量和跨来源标识。', descriptionEn: 'Adds abstracts, citation counts, and cross-source identifiers.', credentialMode: 'optional', testQuery: 'structural engineering' },
  { id: 'arxiv', zh: 'arXiv', en: 'arXiv', descriptionZh: '预印本实时检索，无需 API Key。', descriptionEn: 'Live preprint search with no API key required.', credentialMode: 'none', testQuery: 'machine learning' },
  { id: 'pubmed', zh: 'PubMed', en: 'PubMed', descriptionZh: '生命科学与医学 E-utilities 检索，可配置密钥池。', descriptionEn: 'Life-science and medical E-utilities search with an optional key pool.', credentialMode: 'optional', testQuery: 'cancer immunotherapy' },
  { id: 'crossref', zh: 'Crossref', en: 'Crossref', descriptionZh: 'DOI 元数据与出版信息，无需 API Key。', descriptionEn: 'DOI and publication metadata with no API key required.', credentialMode: 'none', testQuery: 'structural engineering' },
  { id: 'europepmc', zh: 'Europe PMC', en: 'Europe PMC', descriptionZh: '生物医学论文与开放全文线索，无需 API Key。', descriptionEn: 'Biomedical papers and open-full-text signals with no API key required.', credentialMode: 'none', testQuery: 'protein structure' },
  { id: 'hal', zh: 'HAL', en: 'HAL', descriptionZh: '欧洲开放学术仓储，无需 API Key。', descriptionEn: 'European open research repository with no API key required.', credentialMode: 'none', testQuery: 'finite element analysis' },
  { id: 'core', zh: 'CORE', en: 'CORE', descriptionZh: '聚合开放获取仓储，需要 API Key。', descriptionEn: 'Aggregated open-access repositories; requires an API key.', credentialMode: 'required', testQuery: 'structural engineering' },
  { id: 'base', zh: 'BASE', en: 'BASE', descriptionZh: '学术搜索聚合源，无需 API Key。', descriptionEn: 'Academic search aggregator with no API key required.', credentialMode: 'none', testQuery: 'structural engineering' },
  { id: 'sciverse', zh: 'Sciverse', en: 'Sciverse', descriptionZh: '补充出版商聚合检索，需要令牌并支持轮询。', descriptionEn: 'Publisher-aggregated retrieval; requires rotating tokens.', credentialMode: 'required', testQuery: 'structural engineering' },
  { id: 'easyscholar', zh: 'EasyScholar', en: 'EasyScholar', descriptionZh: '期刊等级与评价指标，只参与指标增强。', descriptionEn: 'Journal rankings and metrics; used only for venue enrichment.', credentialMode: 'required', metricOnly: true, testQuery: 'Nature' },
];

export const SEARCH_SOURCES = LITERATURE_SOURCES.filter((source) => !source.metricOnly);
export const CREDENTIAL_SOURCES = LITERATURE_SOURCES.filter(
  (source) => source.credentialMode !== 'none',
);

export const SCORE_DIMENSIONS = [
  { id: 'relevance', zh: '主题相关度', en: 'Relevance' },
  { id: 'evidence_quality', zh: '证据质量', en: 'Evidence quality' },
  { id: 'impact', zh: '学术影响力', en: 'Impact' },
  { id: 'novelty', zh: '新颖度', en: 'Novelty' },
  { id: 'recency', zh: '时效性', en: 'Recency' },
] as const;

export interface LiteratureSettingsDraft {
  sources: string[];
  requested_count: number;
  candidate_budget: number;
  start_year: number | null;
  end_year: number | null;
  score_weights: Record<string, number>;
}

export function draftFrom(settings: LiteratureSearchSettings): LiteratureSettingsDraft {
  return {
    sources: SEARCH_SOURCES.filter((source) => settings.sources.includes(source.id)).map(
      (source) => source.id,
    ),
    requested_count: settings.requested_count,
    candidate_budget: settings.candidate_budget,
    start_year: settings.start_year,
    end_year: settings.end_year,
    score_weights: Object.fromEntries(
      SCORE_DIMENSIONS.map((dimension) => [dimension.id, settings.score_weights[dimension.id] ?? 0]),
    ),
  };
}

export function validateLiteratureSettingsDraft(draft: LiteratureSettingsDraft): string | null {
  if (draft.sources.length === 0) return 'sources';
  if (!Number.isInteger(draft.requested_count) || draft.requested_count < 1 || draft.requested_count > 200) return 'requested_count';
  if (!Number.isInteger(draft.candidate_budget) || draft.candidate_budget < 1 || draft.candidate_budget > 1000) return 'candidate_budget';
  if (draft.candidate_budget < draft.requested_count) return 'candidate_budget_lt_requested';
  if (draft.start_year !== null && (draft.start_year < 1800 || draft.start_year > 3000)) return 'start_year';
  if (draft.end_year !== null && (draft.end_year < 1800 || draft.end_year > 3000)) return 'end_year';
  if (draft.start_year !== null && draft.end_year !== null && draft.start_year > draft.end_year) return 'year_window';
  const weights = SCORE_DIMENSIONS.map((dimension) => draft.score_weights[dimension.id] ?? 0);
  if (weights.some((weight) => !Number.isFinite(weight) || weight < 0)) return 'score_weights';
  if (weights.reduce((total, weight) => total + weight, 0) <= 0) return 'score_weights';
  return null;
}

export function buildLiteratureSettingsUpdate(
  draft: LiteratureSettingsDraft,
): LiteratureSearchSettingsUpdate {
  return {
    sources: [...draft.sources],
    requested_count: draft.requested_count,
    candidate_budget: draft.candidate_budget,
    start_year: draft.start_year,
    end_year: draft.end_year,
    score_weights: { ...draft.score_weights },
  };
}

export function sourceById(id: string): SourceDefinition {
  return LITERATURE_SOURCES.find((source) => source.id === id) ?? {
    id,
    zh: id,
    en: id,
    descriptionZh: '自定义检索来源。',
    descriptionEn: 'Custom literature provider.',
    credentialMode: 'optional',
    testQuery: 'research',
  };
}
