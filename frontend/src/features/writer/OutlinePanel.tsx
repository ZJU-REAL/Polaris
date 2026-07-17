import { useMemo } from 'react';
import { Icon } from '../../components/ui/Icon';

/* ============================================================
   章节大纲面板（左栏下半）：解析当前文件的 \section /
   \subsection / abstract 环境，逐节显示标题、字数与完成度
   （占位符「待撰写」= 未完成），点击跳到对应行。
   ============================================================ */

export interface OutlineEntry {
  /** 1 = section / abstract，2 = subsection */
  level: 1 | 2;
  title: string;
  /** 1-indexed，编辑器跳行用 */
  line: number;
  words: number;
  pending: boolean;
}

const HEADING_RE = /^\s*\\(section|subsection)\*?\{([^}]*)\}/;
const ABSTRACT_RE = /^\s*\\begin\{abstract\}/;
const PLACEHOLDER_RE = /待撰写|to be drafted/;

/** 粗略字数：剥掉注释与命令后，CJK 按字、拉丁按词。 */
function countWords(lines: string[]): number {
  let cjk = 0;
  let latin = 0;
  for (const raw of lines) {
    const line = raw
      .replace(/(^|[^\\])%.*$/, '$1')
      .replace(/\\[a-zA-Z@]+\*?(\[[^\]]*\])?/g, ' ')
      .replace(/[{}$&_^~\\]/g, ' ');
    cjk += line.match(/[一-鿿]/g)?.length ?? 0;
    latin += line.match(/[A-Za-z0-9][A-Za-z0-9'-]*/g)?.length ?? 0;
  }
  return cjk + latin;
}

export function parseOutline(content: string): OutlineEntry[] {
  const lines = content.split('\n');
  const heads: { level: 1 | 2; title: string; line: number }[] = [];
  lines.forEach((text, i) => {
    const m = HEADING_RE.exec(text);
    if (m) {
      heads.push({ level: m[1] === 'section' ? 1 : 2, title: m[2] ?? '', line: i + 1 });
    } else if (ABSTRACT_RE.test(text)) {
      heads.push({ level: 1, title: 'Abstract', line: i + 1 });
    }
  });
  return heads.map((h, idx) => {
    // 统计到下一个同级或更高级标题为止（section 字数含其 subsection）
    let end = lines.length;
    for (let j = idx + 1; j < heads.length; j += 1) {
      if (heads[j]!.level <= h.level) {
        end = heads[j]!.line - 1;
        break;
      }
    }
    const span = lines.slice(h.line - 1, end);
    const words = countWords(span);
    const pending = words < 5 || span.some((l) => PLACEHOLDER_RE.test(l));
    return { ...h, words, pending };
  });
}

export interface OutlinePanelProps {
  /** 当前文件内容（未就绪时为 null） */
  content: string | null;
  open: boolean;
  onToggle: () => void;
  onJump: (line: number) => void;
}

export function OutlinePanel({ content, open, onToggle, onJump }: OutlinePanelProps) {
  const entries = useMemo(() => (content ? parseOutline(content) : []), [content]);
  const totalWords = useMemo(
    () => entries.filter((e) => e.level === 1).reduce((s, e) => s + e.words, 0),
    [entries],
  );

  return (
    <div
      style={{
        borderTop: '0.5px solid var(--border)',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        flex: open ? '0 1 auto' : '0 0 auto',
        maxHeight: open ? '45%' : undefined,
      }}
    >
      <div className="row" style={{ padding: '8px 12px 6px', justifyContent: 'space-between', flexShrink: 0 }}>
        <button
          className="row gap6"
          onClick={onToggle}
          style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', color: 'var(--text-3)' }}
          title={open ? '收起大纲' : '展开大纲'}
        >
          <Icon name="chevDown" size={10} style={{ transform: open ? 'none' : 'rotate(-90deg)', transition: 'transform .12s' }} />
          <span style={{ fontSize: 11, fontWeight: 650, letterSpacing: '0.04em' }}>大纲 · OUTLINE</span>
        </button>
        {open && entries.length > 0 && (
          <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>{totalWords} 词</span>
        )}
      </div>
      {open && (
        <div className="scroll" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '0 8px 10px' }}>
          {entries.length === 0 ? (
            <div style={{ fontSize: 11, color: 'var(--text-4)', padding: '2px 6px' }}>
              {content == null ? '等待编辑器加载…' : '当前文件没有章节标题'}
            </div>
          ) : (
            entries.map((e, i) => (
              <div
                key={`${e.line}-${i}`}
                className="row gap6 writer-file"
                onClick={() => onJump(e.line)}
                title={`跳到第 ${e.line} 行`}
                style={{
                  padding: '4px 8px',
                  paddingLeft: e.level === 2 ? 22 : 8,
                  borderRadius: 7,
                  cursor: 'pointer',
                  color: 'var(--text-2)',
                }}
              >
                <span
                  title={e.pending ? '还没写（占位）' : '已有内容'}
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: '50%',
                    flexShrink: 0,
                    background: e.pending ? 'transparent' : 'var(--ok)',
                    border: e.pending ? '1.5px solid var(--text-4)' : 'none',
                  }}
                />
                <span
                  style={{
                    flex: 1,
                    minWidth: 0,
                    fontSize: 11.5,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    fontWeight: e.level === 1 ? 600 : 500,
                  }}
                >
                  {e.title}
                </span>
                <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)', flexShrink: 0 }}>
                  {e.words}
                </span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
