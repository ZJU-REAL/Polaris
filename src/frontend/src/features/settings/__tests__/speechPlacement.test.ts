import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const settingsSource = readFileSync(
  fileURLToPath(new URL('../SettingsPage.tsx', import.meta.url)),
  'utf-8',
);

const llmTab = settingsSource.slice(
  settingsSource.indexOf('export function LlmTab()'),
  settingsSource.indexOf('// ---------------- 我的模型'),
);

describe('speech model administration placement', () => {
  it('places speech configuration directly after the embedding model', () => {
    const embedding = llmTab.indexOf('<EmbeddingSpaceSection />');
    const speech = llmTab.indexOf('<AdminSpeechSettings />');

    expect(embedding).toBeGreaterThan(-1);
    expect(speech).toBeGreaterThan(embedding);
  });

  it('does not expose speech model administration as its own tab', () => {
    // 管理页并入设置页后（#755），原来的「不单开一个管理标签」要改到这里断言：
    // 语音模型配置只出现在模型与路由这一块里，不是顶层的一个标签。
    // 顶层那个 'speech' 是个人的「语音听读」偏好（PersonalSpeechSettings），两回事。
    const occurrences = settingsSource.split('<AdminSpeechSettings />').length - 1;
    expect(occurrences).toBe(1);
    expect(llmTab).toContain('<AdminSpeechSettings />');
    expect(settingsSource).not.toContain("effectiveTab === 'adminspeech'");
  });
});
