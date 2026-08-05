import type { CSSProperties } from 'react';

/** 限行：超出末尾自动省略号。

    论文标题长短差异极大，不限行的列表会参差不齐、扫读时找不到节奏；限两行是
    「看得出说的是什么」和「每行等高」之间的折中。`overflowWrap: anywhere` 是给
    超长 token（DOI、无空格的模型名）留的后路，否则它们会把行撑出容器。 */
export const clampLines = (lines: number): CSSProperties => ({
  display: '-webkit-box',
  WebkitLineClamp: lines,
  WebkitBoxOrient: 'vertical',
  overflow: 'hidden',
  overflowWrap: 'anywhere',
});
