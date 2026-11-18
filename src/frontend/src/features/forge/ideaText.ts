/* ============================================================
   想法正文的可读性整理（纯函数，渲染前过一遍 markdown 源文）。

   模型写「方法概述 / 风险」这类小节时，常把几条并列的内容挤在一段里：
   「……分三步：1) 先做 A；2) 再做 B；3) 最后做 C。」——一大段读起来很累。
   这里把**明确是编号列举**的一段拆成「引导句 + 列表」，其余文字一律不动。

   判定从严（宁可不拆，也不要把正常句子拆坏）：
   - 编号必须从 1（或 a）开始、逐个递增、至少两项；
   - 编号前面只能是行首、空白或标点（「式(1)」「图(2)」这种引用不算）；
   - 每一项都要有实际内容（至少 4 个字符）；
   - 标题、已有的列表、表格、引用、代码块，以及含行内代码 / 公式的行不处理。
   ============================================================ */

/** 编号写法：`1)` `1）` `(1)` `（1）` `①`；字母只认带完整括号的 `(a)` `（a）`。 */
const MARKER_RE =
  /(^|[\s：:；;，,。.、])(?:[(（](\d{1,2})[)）]|(\d{1,2})[)）]|([①②③④⑤⑥⑦⑧⑨⑩])|[(（]([a-h])[)）])[ \t]*/g;

const CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩';
const MIN_ITEM_CHARS = 4;

type MarkerKind = 'num' | 'alpha';

interface Marker {
  kind: MarkerKind;
  /** 序号：数字编号是 1..n，字母是 a=1.. */
  ordinal: number;
  /** 字母编号的原字母（列表里保留给读者看） */
  letter?: string;
  /** 编号本身（不含前面那个标点）在行内的起点 */
  start: number;
  /** 这一项正文的起点 */
  textStart: number;
}

function findMarkers(line: string): Marker[] {
  const out: Marker[] = [];
  const re = new RegExp(MARKER_RE.source, 'g');
  let m: RegExpExecArray | null;
  while ((m = re.exec(line))) {
    const lead = m[1] ?? '';
    const start = m.index + lead.length;
    const textStart = m.index + m[0].length;
    if (m[5] !== undefined) {
      out.push({ kind: 'alpha', ordinal: m[5].charCodeAt(0) - 96, letter: m[5], start, textStart });
    } else if (m[4] !== undefined) {
      out.push({ kind: 'num', ordinal: CIRCLED.indexOf(m[4]) + 1, start, textStart });
    } else {
      out.push({ kind: 'num', ordinal: Number(m[2] ?? m[3]), start, textStart });
    }
  }
  return out;
}

/** 从第一个「序号 1」起取最长的连续递增段；不足两项返回 null。 */
function enumeratedRun(markers: Marker[]): Marker[] | null {
  for (const kind of ['num', 'alpha'] as const) {
    const same = markers.filter((m) => m.kind === kind);
    const first = same.findIndex((m) => m.ordinal === 1);
    if (first < 0) continue;
    const run = [same[first]!];
    for (let i = first + 1; i < same.length; i += 1) {
      if (same[i]!.ordinal === run.length + 1) run.push(same[i]!);
      else break;
    }
    if (run.length >= 2) return run;
  }
  return null;
}

const SKIP_LINE_RE = /^\s*(?:#{1,6}\s|[-*+]\s|\d{1,3}[.、]\s|>|\||```|-{3,}\s*$)/;

/** 一行文字里的行内编号列举 → 「引导句 + 列表」的若干行；不是列举则返回 null。 */
export function splitEnumeratedLine(line: string): string[] | null {
  if (SKIP_LINE_RE.test(line) || line.includes('`') || line.includes('$')) return null;
  const run = enumeratedRun(findMarkers(line));
  if (!run) return null;

  const items: string[] = [];
  let semicolonSeparated = false;
  for (let i = 0; i < run.length; i += 1) {
    const end = i + 1 < run.length ? run[i + 1]!.start : line.length;
    const raw = line.slice(run[i]!.textStart, end).trim();
    if (i + 1 < run.length && /[；;]$/.test(raw)) semicolonSeparated = true;
    // 项与项之间的分隔符（分号 / 逗号 / 顿号）不带进列表项
    const text = raw.replace(/[；;，,、]+$/, '').trim();
    if (text.length < MIN_ITEM_CHARS) return null;
    items.push(text);
  }

  // 列举后面常跟着一句不属于最后一项的话：「…；3) 做 C。判别器不需要人工标注。」
  // 各项之间用分号隔开时，最后一项里的第一个句号就是列举的结尾，后面的另起一段。
  let tail = '';
  if (semicolonSeparated) {
    const last = items[items.length - 1]!;
    const stop = /[。！？]|[.!?](?=\s)/.exec(last);
    if (stop && stop.index + 1 < last.length) {
      const rest = last.slice(stop.index + 1).trim();
      const kept = last.slice(0, stop.index + 1).trim();
      if (rest && kept.length >= MIN_ITEM_CHARS) {
        items[items.length - 1] = kept;
        tail = rest;
      }
    }
  }

  const lead = line.slice(0, run[0]!.start).trim();
  const out: string[] = [];
  if (lead) out.push(lead, '');
  run.forEach((marker, i) => {
    out.push(marker.kind === 'alpha' ? `- (${marker.letter}) ${items[i]}` : `${i + 1}. ${items[i]}`);
  });
  if (tail) out.push('', tail);
  return out;
}

/** 整篇 markdown：逐行把行内编号列举拆成列表，代码块原样保留。 */
export function splitInlineEnumerations(source: string): string {
  if (!source) return source;
  const lines = source.split('\n');
  const out: string[] = [];
  let inFence = false;
  let changed = false;
  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      out.push(line);
      continue;
    }
    const split = inFence ? null : splitEnumeratedLine(line);
    if (!split) {
      out.push(line);
      continue;
    }
    changed = true;
    // 列表前后各留一个空行，免得和相邻段落粘成一段
    if (out.length > 0 && out[out.length - 1]!.trim() !== '' && split[0] !== '' && /^(\d+\.|- )/.test(split[0]!)) {
      out.push('');
    }
    out.push(...split, '');
  }
  if (!changed) return source;
  // 收掉因此多出来的连续空行和文末空行
  return out.join('\n').replace(/\n{3,}/g, '\n\n').replace(/\n+$/, source.endsWith('\n') ? '\n' : '');
}
