/**
 * 论文来源输入的解析（手动添加文献的公共口径）：
 * 课题「相关研究」的手动添加与「我的文献库」的添加文献共用一份，
 * 保证两处对同一段粘贴内容的判断一致。
 */

/** 解析结果：arXiv 编号或 DOI（字段形状同时兼容书架 import 与论文 import 两个接口）。 */
export type PaperRefInput = { arxiv_id: string } | { doi: string };

/** DOI（10.xxx / doi.org 链接）之外一律按 arXiv 编号处理；空输入返回 null。 */
export function parsePaperRef(raw: string): PaperRefInput | null {
  let v = raw.trim();
  if (!v) return null;
  if (v.includes('doi.org/')) v = v.slice(v.indexOf('doi.org/') + 'doi.org/'.length);
  if (/^10\.\d{4,}/.test(v)) return { doi: v };
  if (v.includes('arxiv.org/')) {
    const seg = v.split('/').filter(Boolean);
    v = (seg[seg.length - 1] ?? '').replace(/\.pdf$/, '');
  }
  return v ? { arxiv_id: v } : null;
}
