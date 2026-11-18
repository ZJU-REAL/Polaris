import { useEffect, useLayoutEffect, useRef, type MutableRefObject } from 'react';
import { Icon } from '../../components/ui/Icon';
import { tr } from '../../lib/i18n';

/* ============================================================
   「想研究什么？」那一个输入框：整页最显眼的东西，打开就能直接打字。
   一个带边框的面：上面是文本，下面一行是两个按钮（主：开始研究；次：调整范围）。
   Enter 开始，Shift + Enter 换行；输入法选词时的 Enter 不算。
   ============================================================ */

export const DIRECTION_MAX = 2000;

const DRAFT_KEY = 'polaris.research.draft';

/** 没发出去的草稿留在本次会话里：连不上引擎、刷新、点去别的页面再回来都还在。 */
export function loadDraft(): string {
  try {
    return sessionStorage.getItem(DRAFT_KEY) ?? '';
  } catch {
    return '';
  }
}

export function saveDraft(text: string): void {
  try {
    if (text) sessionStorage.setItem(DRAFT_KEY, text);
    else sessionStorage.removeItem(DRAFT_KEY);
  } catch {
    /* 存不了就不存 */
  }
}

export interface DirectionBoxProps {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onAdjust: () => void;
  /** 正在创建研究 */
  submitting: boolean;
  /** 正在生成范围（点了「调整范围」之后） */
  drafting: boolean;
  /** 范围编辑器已打开：按钮交给编辑器，这里不再放 */
  adjusting: boolean;
  /** 不能开始（如还没连上模型）：主按钮置灰，原因由页面在框下面说 */
  blocked?: boolean;
  /** 页面要把光标放回框里时用（点了例子、取消了范围编辑） */
  inputRef?: MutableRefObject<HTMLTextAreaElement | null>;
}

export function DirectionBox({
  value,
  onChange,
  onSubmit,
  onAdjust,
  submitting,
  drafting,
  adjusting,
  blocked,
  inputRef,
}: DirectionBoxProps) {
  const ownRef = useRef<HTMLTextAreaElement | null>(null);
  const ref = inputRef ?? ownRef;

  // 打开页面光标就在框里
  useEffect(() => {
    ref.current?.focus();
  }, [ref]);

  // 随内容长高，到上限后框内滚动
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`;
  }, [value, ref]);

  const text = value.trim();
  const tooLong = value.length > DIRECTION_MAX;
  const busy = submitting || drafting;
  const canStart = text.length > 0 && !tooLong && !busy && !blocked;

  return (
    <div className={'dr-box' + (adjusting ? ' is-adjusting' : '')}>
      <textarea
        ref={ref}
        className="dr-box-input"
        rows={3}
        value={value}
        disabled={submitting}
        // 范围是按这段方向拟的：编辑范围时方向先锁住，取消后再改
        readOnly={adjusting || drafting}
        aria-label={tr('研究方向', 'Research direction')}
        placeholder={tr('例如 用扩散模型设计蛋白质骨架', 'e.g. diffusion models for protein backbone design')}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          // 输入法正在选词：这个 Enter 是确认候选词，不是提交
          if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing || e.keyCode === 229) return;
          e.preventDefault();
          if (canStart && !adjusting) onSubmit();
        }}
      />
      <div className="dr-box-foot">
        <span className={'dr-box-count' + (tooLong ? ' is-over' : '')}>
          {value.length > DIRECTION_MAX - 200
            ? `${value.length} / ${DIRECTION_MAX}`
            : !adjusting && text.length > 0 && !blocked
              ? tr('Enter 开始 · Shift + Enter 换行', 'Enter to start · Shift + Enter for a new line')
              : ''}
        </span>
        {!adjusting && (
          <div className="row gap6" style={{ flexShrink: 0 }}>
            <button
              type="button"
              className="btn btn-plain sm"
              onClick={onAdjust}
              // 拟范围也要用模型：模型没连上时一样做不了
              disabled={text.length === 0 || tooLong || busy || blocked}
            >
              {drafting ? (
                <>
                  <Icon name="refresh" size={12} style={{ animation: 'spin 1s linear infinite' }} />
                  {tr('正在生成范围…', 'Drafting scope…')}
                </>
              ) : (
                <>
                  <Icon name="sliders" size={12} />
                  {tr('调整范围', 'Adjust scope')}
                </>
              )}
            </button>
            <button type="button" className="btn btn-primary" onClick={onSubmit} disabled={!canStart}>
              {submitting ? tr('正在开始…', 'Starting…') : tr('开始研究', 'Start research')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
