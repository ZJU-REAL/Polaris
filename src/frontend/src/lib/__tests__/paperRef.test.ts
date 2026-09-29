/**
 * 论文标识识别（#821）。与后端 tests/test_paper_identifiers.py 同一组用例——两边的判定
 * 必须一致，否则同一段粘贴内容在表单上是 DOI、到了服务端又成了别的。
 *
 * 以前不是 DOI 的一律当 arXiv 编号：PMID 31452104 会被当成 arXiv 编号提交。
 */
import { describe, expect, it } from 'vitest';
import { parsePaperRef, recognizePaperRef, refKey, refLabel } from '../paper-ref';

describe('recognizePaperRef', () => {
  const cases: [string, ReturnType<typeof recognizePaperRef>][] = [
    ['2401.01234', { kind: 'arxiv', value: '2401.01234' }],
    ['2401.01234v3', { kind: 'arxiv', value: '2401.01234v3' }],
    ['arXiv:2401.01234', { kind: 'arxiv', value: '2401.01234' }],
    ['https://arxiv.org/abs/2401.01234v2', { kind: 'arxiv', value: '2401.01234v2' }],
    ['https://arxiv.org/pdf/2401.01234.pdf', { kind: 'arxiv', value: '2401.01234' }],
    ['hep-th/9901001', { kind: 'arxiv', value: 'hep-th/9901001' }],
    ['math.GT/0309136', { kind: 'arxiv', value: 'math.GT/0309136' }],
    ['10.1038/s41586-020-2649-2', { kind: 'doi', value: '10.1038/s41586-020-2649-2' }],
    ['https://doi.org/10.1016/j.cell.2020.01.001', { kind: 'doi', value: '10.1016/j.cell.2020.01.001' }],
    ['doi:10.1000/xyz', { kind: 'doi', value: '10.1000/xyz' }],
    ['31452104', { kind: 'pmid', value: '31452104' }],
    ['PMID: 31452104', { kind: 'pmid', value: '31452104' }],
    ['https://pubmed.ncbi.nlm.nih.gov/31452104/', { kind: 'pmid', value: '31452104' }],
    ['PMID:0031452104', { kind: 'pmid', value: '31452104' }],
  ];
  it.each(cases)('%s', (raw, expected) => {
    expect(recognizePaperRef(raw)).toEqual(expected);
  });

  it.each(['', '   ', 'hello world', '1234567890', '0', '10.1/'])('does not guess %j as arXiv', (raw) => {
    expect(recognizePaperRef(raw)).toBeNull();
    expect(parsePaperRef(raw)).toBeNull();
  });
});

describe('parsePaperRef', () => {
  it('maps each kind to the import field', () => {
    expect(parsePaperRef('2401.01234')).toEqual({ arxiv_id: '2401.01234' });
    expect(parsePaperRef('10.1/x')).toBeNull();
    expect(parsePaperRef('10.1234/abc')).toEqual({ doi: '10.1234/abc' });
    expect(parsePaperRef('31452104')).toEqual({ pmid: '31452104' });
  });
});

describe('anchor display and dedup', () => {
  it('labels whichever identifier an anchor has', () => {
    expect(refLabel({ arxiv_id: '2401.01234' })).toBe('2401.01234');
    expect(refLabel({ doi: '10.1/x' })).toBe('doi:10.1/x');
    expect(refLabel({ pmid: '123' })).toBe('PMID 123');
  });

  it('treats arXiv versions of the same paper as one', () => {
    expect(refKey({ arxiv_id: '2401.01234v2' })).toBe(refKey({ arxiv_id: '2401.01234' }));
    expect(refKey({ doi: '10.1/ABC' })).toBe(refKey({ doi: '10.1/abc' }));
  });
});
