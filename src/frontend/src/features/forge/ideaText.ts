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
  /(^|[\s：:；;，,。.、])(?:[(（](\d{1,2})[)）]|(\d{1,2})[)）]|([①②③④⑤⑥⑦⑧⑨⑩])|[(（]([a-hA-H])[)）])[ \t]*/g;

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
      out.push({ kind: 'alpha', ordinal: m[5].toLowerCase().charCodeAt(0) - 96, letter: m[5], start, textStart });
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
  // 行首的「1) …」「2）…」是模型手写的编号段落：markdown 只认「1. 」，换过去才会排成列表
  const lines = source.split('\n').map((l) => l.replace(/^(\s{0,3})(\d{1,2})[)）]\s*(?=\S)/, '$1$2. '));
  const out: string[] = [];
  let inFence = false;
  let changed = lines.join('\n') !== source;
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

/* ============================================================
   真实模型输出才看得出来的几样整理（都是纯函数）。
   ============================================================ */

/**
 * 段首的小标签加粗：「阶段一（领域自监督适配）。在目标语料上…」「实验 1（主对比）：固定…」
 * 「缓解：扩充…」。真实的方案里一节常有五六段这样的并列段落，每段四五行，不加粗时
 * 是一面墙，看不出每段在讲什么。
 *
 * 只认两种写法，从严：
 * - 「标签：正文」——标签不超过 24 个字符、不含逗号句号；
 * - 「标签（说明）。正文」——带括号说明、以句号收尾（「阶段一（…）。」这一类）。
 * 已有 markdown 记号（标题、列表、加粗、表格、代码、公式）的行不动。
 */
// 标签 = 不含标点的短头（≤12 个字符，可以有空格：「实验 1」）+ 可选的一个括号说明（里面可以有逗号）
const LABEL_HEAD = '[^\\s：:，,。；;、！？!?（）()「」*`$|#>\\-][^：:，,。；;、！？!?（）()「」*`$|]{0,11}';
const LABEL_NOTE = '[（(][^）)\\n]{1,32}[）)]';
const LEAD_COLON_RE = new RegExp(`^(${LABEL_HEAD}(?:${LABEL_NOTE})?)([：:])(?=\\s*\\S)`);
const LEAD_PAREN_RE = new RegExp(`^(${LABEL_HEAD}${LABEL_NOTE})([。.])(?=\\s*\\S)`);

export function boldLeadLabels(source: string): string {
  if (!source) return source;
  let inFence = false;
  let prevBlank = true;
  return source
    .split('\n')
    .map((line) => {
      if (/^\s*```/.test(line)) {
        inFence = !inFence;
        prevBlank = false;
        return line;
      }
      const startsParagraph = prevBlank;
      prevBlank = line.trim() === '';
      if (inFence || !startsParagraph || SKIP_LINE_RE.test(line)) return line;
      // 整段很短的一句话（「核心假设：X。」）不是并列段落的小标题，不加粗
      if (line.length < 40) return line;
      const m = LEAD_COLON_RE.exec(line) ?? LEAD_PAREN_RE.exec(line);
      if (!m) return line;
      const label = m[1]!;
      // 网址（http:）不是标签
      if (/^https?$/i.test(label)) return line;
      return `**${label}${m[2]}**${line.slice(m[0].length)}`;
    })
    .join('\n');
}

export interface BodySection {
  /** 锚点 id（页面内唯一） */
  id: string;
  /** 小节标题；正文开头没有标题的那一段为空串 */
  title: string;
  /** 这一节的 markdown（不含标题行） */
  body: string;
  /** 大致字数（提示「展开」后有多长） */
  chars: number;
}

/**
 * 想法正文按最高一级标题切成小节（动机 / 方法概述 / 预期实验 / 风险…），
 * 详情页据此出目录、给长的小节加「展开」。代码块里的 # 不算标题。
 */
export function splitBodySections(source: string): BodySection[] {
  if (!source || !source.trim()) return [];
  const lines = source.split('\n');
  let inFence = false;
  let top = 7;
  for (const line of lines) {
    if (/^\s*```/.test(line)) inFence = !inFence;
    if (inFence) continue;
    const m = /^(#{1,6})\s+\S/.exec(line);
    if (m) top = Math.min(top, m[1]!.length);
  }
  const out: BodySection[] = [];
  let cur: { title: string; lines: string[] } = { title: '', lines: [] };
  const push = () => {
    const body = cur.lines.join('\n').trim();
    if (!cur.title && !body) return;
    out.push({ id: `sec-${out.length + 1}`, title: cur.title, body, chars: body.replace(/\s+/g, '').length });
  };
  const heading = top <= 6 ? new RegExp(`^#{${top}}\\s+(.+?)\\s*#*\\s*$`) : null;
  inFence = false;
  for (const line of lines) {
    if (/^\s*```/.test(line)) inFence = !inFence;
    const m = !inFence && heading ? heading.exec(line) : null;
    if (m) {
      push();
      cur = { title: m[1]!.replace(/\*\*/g, '').trim(), lines: [] };
    } else cur.lines.push(line);
  }
  push();
  return out;
}

export interface CleanSnippet {
  text: string;
  /** 开头是从句子中间截的（前面加了省略号） */
  cutStart: boolean;
  cutEnd: boolean;
}

const LIGATURES: Record<string, string> = { ﬁ: 'fi', ﬂ: 'fl', ﬀ: 'ff', ﬃ: 'ffi', ﬄ: 'ffl' };

/**
 * 论文原文片段的整理。片段是从 PDF 抽的文字里按长度截出来的：行尾是硬换行、
 * 单词被连字符拆成两半（demonstrat-\ning）、有连字（ﬁ）、开头和结尾常落在词或句子中间。
 * 这里把它接回成一段能读的文字：换行并成空格、拆开的词接上、两端截断的残词去掉并
 * 标上省略号。不改写内容。
 */
export function cleanSnippet(raw: string | null | undefined, title?: string | null): CleanSnippet {
  let t = (raw ?? '').replace(/[ﬁﬂﬀﬃﬄ]/g, (c) => LIGATURES[c] ?? c);
  if (!t.trim()) return { text: '', cutStart: false, cutEnd: false };
  // 取自论文开头的片段以标题起头（标题就在它上面一行）：不重复一遍
  const head = (title ?? '').trim();
  if (head.length > 12) {
    const norm = (x: string) => x.replace(/\s+/g, ' ').trim().toLowerCase();
    const flat = t.replace(/\s+/g, ' ').trim();
    if (norm(flat).startsWith(norm(head))) t = flat.slice(head.replace(/\s+/g, ' ').length).replace(/^[\s:：.\-–]+/, '');
  }
  // 行尾连字符：两边都是小写字母才接（行内的 query-side 这类真连字符不受影响）
  t = t.replace(/([a-z])-\n([a-z])/g, '$1$2');
  // 列表符号单独占一行（「•\nRevela learns…」）：并到下一行
  t = t.replace(/\n\s*([•·▪◦])\s*\n/g, '\n$1 ');
  t = t.replace(/\s*\n\s*/g, ' ').replace(/[ \t]{2,}/g, ' ').trim();

  // 开头：以小写字母或标点开头 = 从句子中间截的。丢掉第一个残词，标省略号
  let cutStart = false;
  if (/^[a-z,;:)\]’'”"]/.test(t)) {
    cutStart = true;
    const sp = t.indexOf(' ');
    // 第一个词很可能是半个词（inations）；只有它后面还有足够内容时才丢
    if (sp > 0 && sp < 24 && t.length - sp > 40) t = t.slice(sp + 1);
  }
  // 结尾：不以句末标点收尾 = 截断。丢掉最后一个残词，标省略号
  let cutEnd = false;
  if (!/[.!?。！？”"’')\]]$/.test(t)) {
    cutEnd = true;
    const sp = t.lastIndexOf(' ');
    if (sp > 40 && t.length - sp < 24) t = t.slice(0, sp);
    t = t.replace(/[,;:，；：\-–(（]+$/, '');
  }
  return { text: `${cutStart ? '…' : ''}${t}${cutEnd ? '…' : ''}`, cutStart, cutEnd };
}

/**
 * 评分理由里漏出来的内部字段名换成人话。模型写理由时会直接引用它看到的数据字段
 * （support_ratio、novel_ratio、risk_note…）；这些词对用户没有意义。只换认得的那几个，
 * 其余原样。
 */
const FIELD_WORDS: [RegExp, string, string][] = [
  [/\bsupport[_ ]ratio\b/gi, '文献支持比例', 'share of supported claims'],
  [/\bnovel(?:ty)?[_ ]ratio\b/gi, '新颖论断比例', 'share of novel claims'],
  [/\brisk[_ ]note\b/gi, '风险说明', 'risk note'],
  [/\bpipeline[_ ]score\b/gi, '综合评分', 'overall score'],
  [/\bgrounded[_ ]paper[_ ]count\b/gi, '支持论文数', 'supporting papers'],
  [/\brelated[_ ]chunk[_ ]hits\b/gi, '相关段落数', 'related passages'],
  [/\bunsupported[_ ]subclaims\b/gi, '尚无依据的论断', 'unsupported claims'],
];

export function humanizeFieldNames(text: string): string {
  if (!text) return text;
  // 换成正文自己的语言（正文是中文就用中文词，和界面语言无关）
  const zhText = /[\u4e00-\u9fff]/.test(text);
  let out = text;
  for (const [re, zh, en] of FIELD_WORDS) out = out.replace(re, zhText ? zh : en);
  return out;
}
