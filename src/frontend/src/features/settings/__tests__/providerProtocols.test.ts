/**
 * Provider 协议选择（#809）。
 *
 * 后端加了一种协议而前端下拉框里没有，等于这种协议不存在——这是这个仓库里反复
 * 出现的那类缺陷：能力做完了，入口没有。这里钉住「后端认的协议，前端都给得出」。
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { KIND_LABELS } from '../SettingsPage';

const backendSchema = readFileSync(
  join(__dirname, '..', '..', '..', '..', '..', 'backend', 'app', 'schemas', 'llm_admin.py'),
  'utf8',
);

/** 后端 ProviderKind Literal 里列出的协议。 */
function backendKinds(): string[] {
  const line = backendSchema.split('\n').find((l) => l.startsWith('ProviderKind = Literal['));
  if (!line) throw new Error('ProviderKind not found in llm_admin.py');
  return [...line.matchAll(/"([a-z_]+)"/g)].map((m) => m[1] ?? '');
}

describe('provider protocols', () => {
  it('每个后端协议都有显示名', () => {
    for (const kind of backendKinds()) {
      expect(Object.keys(KIND_LABELS)).toContain(kind);
    }
  });

  it('Responses API 可选，且显示的是协议名而不是内部 id', () => {
    expect(KIND_LABELS.openai_responses).toBe('OpenAI Responses');
    expect(KIND_LABELS.openai_compat).toBe('OpenAI Chat Completions');
    expect(KIND_LABELS.anthropic).toBe('Anthropic Messages');
  });
});
