/**
 * 论文标识的识别（手动添加文献与锚点论文的公共口径，#821）。
 *
 * 与后端 services/paper_identifiers.py 同一套规则：arXiv 编号、DOI、PMID 各有明确
 * 形状，认不出的就返回 null。以前不是 DOI 的一律当 arXiv 编号——一个 PMID
 * （31452104）会被当成 arXiv 编号提交，然后报「arXiv 上查不到」。
 */

export type PaperRefKind = 'arxiv' | 'doi' | 'pmid';

export interface PaperRef {
  kind: PaperRefKind;
  value: string;
}

/** 解析结果：字段形状同时兼容书架 import 与论文 import 两个接口。 */
export type PaperRefInput = { arxiv_id: string } | { doi: string } | { pmid: string };

const ARXIV_NEW = /^\d{4}\.\d{4,5}(v\d+)?$/;
const ARXIV_OLD = /^[a-z][a-z-]*(\.[a-z]{2})?\/\d{7}(v\d+)?$/i;
const DOI = /^10\.\d{4,9}\/\S+$/;
const PMID = /^\d{1,9}$/;

function pmidOf(raw: string): string | null {
  let v = raw.trim();
  const url = v.match(/pubmed\.ncbi\.nlm\.nih\.gov\/(\d+)/i);
  if (url?.[1]) v = url[1];
  v = v.replace(/^pmid\s*:?\s*/i, '');
  if (!/^\d+$/.test(v)) return null;
  // 前导零不算位数；全零不是编号
  const digits = v.replace(/^0+/, '');
  return digits && PMID.test(digits) ? digits : null;
}

/** 一段粘贴内容 → 标识类型与规范值；认不出返回 null。判定顺序 DOI → arXiv → PMID。 */
export function recognizePaperRef(raw: string): PaperRef | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const lower = trimmed.toLowerCase();

  let v = trimmed;
  if (lower.includes('doi.org/')) v = trimmed.slice(lower.indexOf('doi.org/') + 'doi.org/'.length);
  else if (lower.startsWith('doi:')) v = trimmed.slice(4).trim();
  if (DOI.test(v)) return { kind: 'doi', value: v };

  v = trimmed;
  if (lower.includes('arxiv.org/')) {
    const tail = trimmed.split(/arxiv\.org\/(?:abs|pdf)\//i).pop() ?? '';
    v = tail.replace(/\/+$/, '').replace(/\.pdf$/i, '');
  } else if (lower.startsWith('arxiv:')) {
    v = trimmed.slice(6).trim();
  }
  if (ARXIV_NEW.test(v) || ARXIV_OLD.test(v)) return { kind: 'arxiv', value: v };

  const pmid = pmidOf(trimmed);
  return pmid ? { kind: 'pmid', value: pmid } : null;
}

export function refInput(ref: PaperRef): PaperRefInput {
  if (ref.kind === 'arxiv') return { arxiv_id: ref.value };
  if (ref.kind === 'doi') return { doi: ref.value };
  return { pmid: ref.value };
}

/** 粘贴内容 → 导入接口的入参；认不出返回 null（调用方据此提示，不要再兜底成 arXiv）。 */
export function parsePaperRef(raw: string): PaperRefInput | null {
  const ref = recognizePaperRef(raw);
  return ref ? refInput(ref) : null;
}

/** 一条带标识的记录（如锚点论文）显示用的标识：arXiv 优先，其次 DOI、PMID。 */
export function refLabel(item: { arxiv_id?: string | null; doi?: string | null; pmid?: string | null }): string {
  if (item.arxiv_id) return item.arxiv_id;
  if (item.doi) return `doi:${item.doi}`;
  if (item.pmid) return `PMID ${item.pmid}`;
  return '';
}

/** 去重用的键。 */
export function refKey(item: { arxiv_id?: string | null; doi?: string | null; pmid?: string | null }): string {
  if (item.arxiv_id) return `arxiv:${item.arxiv_id.replace(/v\d+$/i, '').toLowerCase()}`;
  if (item.doi) return `doi:${item.doi.toLowerCase()}`;
  if (item.pmid) return `pmid:${item.pmid}`;
  return '';
}
