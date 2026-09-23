/**
 * Provider 设置里的 rerank 路径（#810）。
 *
 * 后端要求路径以 / 开头：不带斜杠会拼成 https://host/v1rerank 这种既不报错也永远
 * 打不通的地址。表单替人补上斜杠，而不是让他收到一个 422 再回来改。
 */
import { describe, expect, it } from 'vitest';
import { rerankPathValue } from '../SettingsPage';

describe('rerankPathValue', () => {
  it('留空就是默认 /rerank——默认值不能变，改了会弄坏现在能用的服务', () => {
    expect(rerankPathValue('')).toBe('/rerank');
    expect(rerankPathValue('   ')).toBe('/rerank');
  });

  it('原样保留一个合法路径', () => {
    expect(rerankPathValue('/reranks')).toBe('/reranks');
    expect(rerankPathValue('/v2/rerank')).toBe('/v2/rerank');
  });

  it('漏了开头的斜杠就补上', () => {
    expect(rerankPathValue('reranks')).toBe('/reranks');
  });

  it('去掉首尾空白', () => {
    expect(rerankPathValue('  /reranks  ')).toBe('/reranks');
  });
});
