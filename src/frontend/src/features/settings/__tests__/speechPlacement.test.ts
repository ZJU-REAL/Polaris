import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const settingsSource = readFileSync(
  fileURLToPath(new URL('../SettingsPage.tsx', import.meta.url)),
  'utf-8',
);
const adminSource = readFileSync(
  fileURLToPath(new URL('../AdminSettingsPage.tsx', import.meta.url)),
  'utf-8',
);

describe('speech model administration placement', () => {
  it('places speech configuration directly after the embedding model', () => {
    const llmTab = settingsSource.slice(
      settingsSource.indexOf('export function LlmTab()'),
      settingsSource.indexOf('// ---------------- 我的模型'),
    );
    const embedding = llmTab.indexOf('<EmbeddingSpaceSection />');
    const speech = llmTab.indexOf('<AdminSpeechSettings />');

    expect(embedding).toBeGreaterThan(-1);
    expect(speech).toBeGreaterThan(embedding);
  });

  it('does not expose speech as a separate admin tab', () => {
    expect(adminSource).not.toContain("{ v: 'speech'");
    expect(adminSource).not.toContain("shownTab === 'speech'");
  });
});
