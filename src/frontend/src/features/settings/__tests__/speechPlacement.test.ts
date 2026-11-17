import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const settingsSource = readFileSync(
  fileURLToPath(new URL('../SettingsPage.tsx', import.meta.url)),
  'utf-8',
);

/** 页面渲染里某个标签页的那一段：从它的条件开始，到下一个标签页的条件为止。 */
function tabBranch(tab: string, next: string): string {
  const from = settingsSource.indexOf(`{effectiveTab === '${tab}'`);
  const to = settingsSource.indexOf(`{effectiveTab === '${next}'`, from);
  expect(from).toBeGreaterThan(-1);
  expect(to).toBeGreaterThan(from);
  return settingsSource.slice(from, to);
}

describe('speech service placement', () => {
  it('sits in the speech tab, under the personal read-aloud settings', () => {
    // 以前语音服务配置在「模型与智能体」里；那一页撤掉后，它和朗读偏好放在同一个「语音」标签下
    const branch = tabBranch('speech', 'bots');
    const personal = branch.indexOf('<PersonalSpeechSettings />');
    const service = branch.indexOf('<AdminSpeechSettings />');
    expect(personal).toBeGreaterThan(-1);
    expect(service).toBeGreaterThan(personal);
  });

  it('is rendered exactly once and has no tab of its own', () => {
    const occurrences = settingsSource.split('<AdminSpeechSettings />').length - 1;
    expect(occurrences).toBe(1);
    expect(settingsSource).not.toContain("effectiveTab === 'adminspeech'");
  });
});

describe('what else lived in the removed Models & agents tab', () => {
  it('keeps the call log with usage', () => {
    const from = settingsSource.indexOf('function MyUsageTab()');
    const to = settingsSource.indexOf('// ---------------- 页面', from);
    expect(from).toBeGreaterThan(-1);
    expect(settingsSource.slice(from, to)).toContain('<CallLogsSection />');
    expect(settingsSource.split('<CallLogsSection />').length - 1).toBe(1);
  });

  it('keeps author affiliations with document processing', () => {
    expect(tabBranch('processing', 'experiment')).toContain('<AffiliationModeSection />');
    expect(settingsSource.split('<AffiliationModeSection />').length - 1).toBe(1);
  });
});
