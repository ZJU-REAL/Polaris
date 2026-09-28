import { describe, expect, it } from 'vitest';
import { providerTestCapability } from '../SettingsPage';

/**
 * 供应商表的「模型状态」必须和路由表按同一种能力测（#819）。以前一律按 chat 测：嵌入服务
 * 显示正常、重排序服务显示失败，而路由表按真实能力测出来的恰好相反。
 */
const routes = [
  { stage: 'default', provider_id: 'llm', model: 'qwen' },
  { stage: 'embedding', provider_id: 'emb', model: 'Qwen3-Embedding-4B' },
  { stage: 'rerank', provider_id: 'rr', model: 'bge-reranker-v2-m3' },
];

describe('providerTestCapability', () => {
  it('tests an embedding model as embedding', () => {
    expect(providerTestCapability('emb', 'Qwen3-Embedding-4B', routes)).toBe('embedding');
  });

  it('tests a rerank model as rerank', () => {
    expect(providerTestCapability('rr', 'bge-reranker-v2-m3', routes)).toBe('rerank');
  });

  it('tests anything else as chat', () => {
    expect(providerTestCapability('llm', 'qwen', routes)).toBe('chat');
    expect(providerTestCapability('emb', 'some-other-model', routes)).toBe('chat');
    expect(providerTestCapability('new', 'm', [])).toBe('chat');
  });

  it('matches on provider and model together', () => {
    // 同名模型挂在别的 provider 上，不能被认成嵌入模型
    expect(providerTestCapability('llm', 'Qwen3-Embedding-4B', routes)).toBe('chat');
  });
});
