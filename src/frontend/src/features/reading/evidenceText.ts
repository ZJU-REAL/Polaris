interface TextPosition {
  part: number;
  start: number;
  end: number;
}

export interface NormalizedTextMatch {
  startPart: number;
  startOffset: number;
  endPart: number;
  endOffset: number;
}

export interface EvidenceTextHints {
  sectionPath?: string[] | null;
}

function compact(value: string): string[] {
  return Array.from(value.normalize('NFKC').toLocaleLowerCase()).filter((char) =>
    /[\p{L}\p{N}]/u.test(char),
  );
}

function compactString(value?: string | null): string {
  return compact(value ?? '').join('');
}

function normalizedParts(parts: string[]) {
  const positions: TextPosition[] = [];
  const haystack: string[] = [];
  parts.forEach((part, partIndex) => {
    let offset = 0;
    for (const sourceChar of Array.from(part)) {
      const width = sourceChar.length;
      for (const normalizedChar of compact(sourceChar)) {
        haystack.push(normalizedChar);
        positions.push({ part: partIndex, start: offset, end: offset + width });
      }
      offset += width;
    }
  });
  return { haystack: haystack.join(''), positions };
}

export function findAllNormalizedTextMatches(parts: string[], quote: string): NormalizedTextMatch[] {
  const needle = compactString(quote);
  if (needle.length < 6) return [];
  const { haystack, positions } = normalizedParts(parts);
  const matches: NormalizedTextMatch[] = [];
  let cursor = 0;
  while (cursor <= haystack.length - needle.length) {
    const start = haystack.indexOf(needle, cursor);
    if (start < 0) break;
    const first = positions[start];
    const last = positions[start + needle.length - 1];
    if (first && last) {
      matches.push({
        startPart: first.part,
        startOffset: first.start,
        endPart: last.part,
        endOffset: last.end,
      });
    }
    cursor = start + 1;
  }
  return matches;
}

function textNodes(root: HTMLElement): { nodes: Text[]; sections: string[] } {
  const nodes: Text[] = [];
  const sections: string[] = [];
  let section = '';
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const textNode = node as Text;
    const parent = textNode.parentElement;
    if (!parent || parent.closest('script, style, [aria-hidden="true"], button, input, textarea, select')) continue;
    const heading = parent.closest('h1, h2, h3, h4, h5, h6');
    if (heading?.textContent?.trim()) section = heading.textContent.trim();
    nodes.push(textNode);
    sections.push(section);
  }
  return { nodes, sections };
}

function rangeFor(nodes: Text[], match: NormalizedTextMatch): Range | null {
  const first = nodes[match.startPart];
  const last = nodes[match.endPart];
  if (!first || !last) return null;
  const range = document.createRange();
  range.setStart(first, match.startOffset);
  range.setEnd(last, match.endOffset);
  return range;
}

export function findNormalizedTextRanges(root: HTMLElement, quote: string): Range[] {
  const { nodes } = textNodes(root);
  return findAllNormalizedTextMatches(nodes.map((node) => node.data), quote)
    .map((match) => rangeFor(nodes, match))
    .filter((range): range is Range => range !== null);
}

export function findPreciseNormalizedTextRange(
  root: HTMLElement,
  quote: string,
  hints: EvidenceTextHints = {},
): Range | null {
  const { nodes, sections } = textNodes(root);
  const matches = findAllNormalizedTextMatches(nodes.map((node) => node.data), quote);
  if (matches.length === 1) return rangeFor(nodes, matches[0]!);
  const expected = compactString(hints.sectionPath?.at(-1));
  if (!expected || matches.length === 0) return null;
  const sectionMatches = matches.filter((match) =>
    compactString(sections[match.startPart]).includes(expected),
  );
  return sectionMatches.length === 1 ? rangeFor(nodes, sectionMatches[0]!) : null;
}
