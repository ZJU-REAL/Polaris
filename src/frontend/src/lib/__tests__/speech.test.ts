import { describe, expect, it } from 'vitest';
import { pcm16LeToFloat32, splitSpeechText } from '../speech';

async function playWithProviderLimit(text: string, maxChars: number): Promise<void> {
  for (const part of splitSpeechText(text, maxChars)) {
    if (part.length > maxChars) {
      throw new Error('播放失败：内容太长，请缩短后再播放');
    }
  }
}

describe('splitSpeechText', () => {
  it('keeps a long daily digest within the provider limit without losing text', async () => {
    const digest = [
      '# 今日论文简报',
      '第一篇论文介绍了一个新的训练方法，实验结果显著。',
      '第二篇论文分析了模型对齐问题，并给出了完整结论。',
      '第三篇论文提供了开源代码和进一步研究方向。',
    ].join('\n\n');

    await expect(playWithProviderLimit(digest, 32)).resolves.toBeUndefined();
    expect(splitSpeechText(digest, 32).join('').replace(/\s+/g, '')).toBe(
      digest.replace(/\s+/g, ''),
    );
  });

  it('hard-splits an unbroken string and ignores empty input', () => {
    expect(splitSpeechText('x'.repeat(75), 32).map((part) => part.length)).toEqual([32, 32, 11]);
    expect(splitSpeechText('   ', 32)).toEqual([]);
  });

  it('splits the current 50,920-character digest into safe playback parts', () => {
    const parts = splitSpeechText('研'.repeat(50_920), 500);
    expect(parts).toHaveLength(102);
    expect(parts.every((part) => part.length <= 500)).toBe(true);
  });
});

describe('pcm16LeToFloat32', () => {
  it('decodes signed PCM and carries an odd network byte into the next chunk', () => {
    const first = pcm16LeToFloat32(new Uint8Array([0x00, 0x80, 0xff]));
    expect(Array.from(first.samples)).toEqual([-1]);
    expect(first.trailingByte).toBe(0xff);

    const second = pcm16LeToFloat32(new Uint8Array([0x7f, 0x00, 0x00]), first.trailingByte);
    expect(second.samples[0]).toBe(1);
    expect(second.samples[1]).toBe(0);
    expect(second.trailingByte).toBeNull();
  });
});
