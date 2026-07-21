import { StateEffect, StateField, RangeSet } from '@codemirror/state';
import {
  Decoration,
  EditorView,
  ViewPlugin,
  WidgetType,
  type DecorationSet,
  type ViewUpdate,
} from '@codemirror/view';

/* ============================================================
   AI 光标装饰：AI 起草时在编辑器里显示一个会打字的「AI 光标」
   （标签 + 脉动竖条），并把所在小节整行轻微高亮、自动滚动跟随。
   正文本身由 CRDT 房间实时灌入（服务端流式），本装饰只负责在
   当前小节正文末尾画光标——随文档变化自动重算位置，天然跟随。
   ============================================================ */

export interface AiTarget {
  /** POLARIS_SECTION 标记名，如 introduction / related_work */
  section: string;
  /** typing=撰写；revising=改写/精修 */
  phase: 'typing' | 'revising';
}

/** 设置/清除 AI 光标目标（null = 收起）。 */
export const setAiTarget = StateEffect.define<AiTarget | null>();

const aiTargetField = StateField.define<AiTarget | null>({
  create: () => null,
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setAiTarget)) value = e.value;
    return value;
  },
});

/** 定位小节正文末尾（END 标记行前一个换行处）→ 文档偏移；找不到返回 null。 */
function sectionCaretPos(doc: string, section: string): number | null {
  const begin = new RegExp(`^[ \\t]*%\\s*POLARIS_SECTION:\\s*${escapeRe(section)}[ \\t]*$`, 'm');
  const m = begin.exec(doc);
  if (!m) return null;
  const bodyStart = doc.indexOf('\n', m.index + m[0].length);
  if (bodyStart === -1) return doc.length;
  const end = new RegExp(`^[ \\t]*%\\s*POLARIS_SECTION_END:\\s*${escapeRe(section)}[ \\t]*$`, 'm');
  const em = execFrom(end, doc, bodyStart + 1);
  // 正文末尾 = END 标记行起点（无 END 标记则取下一个分节标记 / 文末）
  const caret = em ? em.index : (nextSectionMarker(doc, bodyStart + 1) ?? doc.length);
  // 落在 END 标记行前一个换行之前，避免光标跑到 END 注释行上
  return caret > 0 && doc[caret - 1] === '\n' ? caret - 1 : caret;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function execFrom(re: RegExp, s: string, from: number): RegExpExecArray | null {
  const r = new RegExp(re.source, 'm');
  r.lastIndex = from;
  // 'm' 无 'g' 时 lastIndex 无效，改用 slice
  const sub = s.slice(from);
  const m = r.exec(sub);
  if (!m) return null;
  m.index += from;
  return m;
}

function nextSectionMarker(doc: string, from: number): number | null {
  const re = /^[ \t]*%\s*POLARIS_SECTION(?:_END)?:/m;
  const sub = doc.slice(from);
  const m = re.exec(sub);
  return m ? from + m.index : null;
}

class AiCaretWidget extends WidgetType {
  constructor(readonly phase: 'typing' | 'revising') {
    super();
  }
  eq(other: AiCaretWidget) {
    return other.phase === this.phase;
  }
  toDOM() {
    const wrap = document.createElement('span');
    wrap.className = 'ai-caret';
    const bar = document.createElement('span');
    bar.className = 'ai-caret-bar';
    const label = document.createElement('span');
    label.className = 'ai-caret-label';
    label.textContent = this.phase === 'revising' ? '✨ AI 修订中' : '✨ AI';
    wrap.appendChild(bar);
    wrap.appendChild(label);
    return wrap;
  }
  ignoreEvent() {
    return true;
  }
}

const activeLineDeco = Decoration.line({ attributes: { class: 'ai-active-line' } });

/** ViewPlugin：按当前 AI target + 文档内容重算光标装饰，并自动滚动跟随。 */
const aiCaretPlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet = Decoration.none;
    lastPos = -1;

    constructor(view: EditorView) {
      this.build(view);
    }

    update(update: ViewUpdate) {
      const targetChanged = update.transactions.some((t) =>
        t.effects.some((e) => e.is(setAiTarget)),
      );
      if (update.docChanged || targetChanged) this.build(update.view);
    }

    build(view: EditorView) {
      const target = view.state.field(aiTargetField, false);
      if (!target) {
        this.decorations = Decoration.none;
        this.lastPos = -1;
        return;
      }
      const pos = sectionCaretPos(view.state.doc.toString(), target.section);
      if (pos == null) {
        this.decorations = Decoration.none;
        this.lastPos = -1;
        return;
      }
      const clamped = Math.min(pos, view.state.doc.length);
      const line = view.state.doc.lineAt(clamped);
      const ranges = [
        activeLineDeco.range(line.from),
        Decoration.widget({ widget: new AiCaretWidget(target.phase), side: 1 }).range(clamped),
      ];
      this.decorations = RangeSet.of(ranges, true);

      // 位置移动时自动滚动跟随（下一帧派发，避免在 update 内同步 dispatch）
      if (clamped !== this.lastPos) {
        this.lastPos = clamped;
        requestAnimationFrame(() => {
          try {
            view.dispatch({ effects: EditorView.scrollIntoView(clamped, { y: 'center' }) });
          } catch {
            /* 视图可能已销毁 */
          }
        });
      }
    }
  },
  { decorations: (v) => v.decorations },
);

/** AI 光标扩展（挂进 CodeMirror）。 */
export const aiCursorExtension = [aiTargetField, aiCaretPlugin];
