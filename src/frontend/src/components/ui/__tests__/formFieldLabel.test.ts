import { afterEach, describe, expect, it } from 'vitest';
import { setLang, tr } from '../../../lib/i18n';
import { fieldLabel } from '../FormField';

/* FormField 的标签：调用方既有「label 传中文 + en 传英文」的旧写法，也有
   「label 已用 tr 翻好 + en 传字段名」的写法。后者在英文界面下不能显示成字段名。 */
describe('fieldLabel', () => {
  afterEach(() => setLang('zh'));

  it('uses the label as is when no en is given', () => {
    setLang('en');
    expect(fieldLabel('Rounds')).toBe('Rounds');
    setLang('zh');
    expect(fieldLabel('轮数')).toBe('轮数');
  });

  it('translates an untranslated Chinese label with en', () => {
    setLang('en');
    expect(fieldLabel('辩论轮数', 'Debate rounds')).toBe('Debate rounds');
    setLang('zh');
    expect(fieldLabel('辩论轮数', 'Debate rounds')).toBe('辩论轮数');
  });

  it('never replaces an already translated label with a field name', () => {
    setLang('en');
    expect(fieldLabel(tr('最多运行轮数', 'Max runs'), 'max_runs')).toBe('Max runs');
    setLang('zh');
    expect(fieldLabel(tr('最多运行轮数', 'Max runs'), 'max_runs')).toBe('最多运行轮数');
  });
});
