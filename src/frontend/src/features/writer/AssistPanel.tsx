import { useEffect, useMemo, useRef, useState } from 'react';
import type { EditorView } from '@codemirror/view';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { assistManuscriptSse, type AssistInput } from '../../lib/sse';
import { tr } from '../../lib/i18n';

/* ============================================================
   内联 AI 面板（编辑器下方停靠条）：
   - 润色 / 改写：对打开面板时捕获的选区操作；
   - 续写：从光标处向后续写；
   - 结果流式展示，人看过后选择替换选区 / 插到光标处 / 重试，
     越界引用/图表以警告条提示（不阻断，人来定）。
   ============================================================ */

export type AssistMode = 'polish' | 'rewrite' | 'continue';

/** 面板标题（渲染时求值，随语言切换） */
function modeTitle(mode: AssistMode): string {
  return mode === 'polish' ? tr('AI 润色', 'AI polish') : mode === 'rewrite' ? tr('AI 改写', 'AI rewrite') : tr('AI 续写', 'AI continue');
}

/** 选区/光标前后文窗口（与后端 MAX_CONTEXT_CHARS 对齐即可，超出后端会截断） */
const CONTEXT_CHARS = 2000;

interface Captured {
  from: number;
  to: number;
  text: string;
  before: string;
  after: string;
}

function captureSelection(view: EditorView): Captured {
  const sel = view.state.selection.main;
  const doc = view.state.doc;
  return {
    from: sel.from,
    to: sel.to,
    text: view.state.sliceDoc(sel.from, sel.to),
    before: view.state.sliceDoc(Math.max(0, sel.from - CONTEXT_CHARS), sel.from),
    after: view.state.sliceDoc(sel.to, Math.min(doc.length, sel.to + CONTEXT_CHARS)),
  };
}

export interface AssistPanelProps {
  manuscriptId: string;
  mode: AssistMode;
  view: EditorView;
  onClose: () => void;
}

type Phase = 'input' | 'streaming' | 'done' | 'error';

export function AssistPanel({ manuscriptId, mode, view, onClose }: AssistPanelProps) {
  // 打开面板那一刻捕获选区（此后编辑器里的选择变化不影响本次任务）
  const captured = useMemo(() => captureSelection(view), [view, mode]);
  const [instruction, setInstruction] = useState('');
  const [phase, setPhase] = useState<Phase>('input');
  const [result, setResult] = useState('');
  const [warnings, setWarnings] = useState<string[]>([]);
  const [errorText, setErrorText] = useState('');
  const abortRef = useRef<(() => void) | null>(null);
  const resultBoxRef = useRef<HTMLDivElement | null>(null);
  // onClose/stop 回调里要读最新结果，state 闭包会过期，用 ref 同步一份
  const resultRef = useRef('');
  resultRef.current = result;

  const needsSelection = mode !== 'continue';
  const selectionMissing = needsSelection && captured.text.trim() === '';

  useEffect(() => () => abortRef.current?.(), []);

  // 流式输出时自动滚到底
  useEffect(() => {
    const box = resultBoxRef.current;
    if (box && phase === 'streaming') box.scrollTop = box.scrollHeight;
  }, [result, phase]);

  function start() {
    if (selectionMissing) return;
    if (mode === 'rewrite' && !instruction.trim()) {
      toast(tr('请先写下改写要求，例如「更简洁」', 'Say how to rewrite it first, e.g. “more concise”'), 'info');
      return;
    }
    setPhase('streaming');
    setResult('');
    setWarnings([]);
    setErrorText('');
    const input: AssistInput = {
      mode,
      text: needsSelection ? captured.text : '',
      instruction: instruction.trim(),
      before: captured.before,
      after: needsSelection ? captured.after : '',
    };
    abortRef.current = assistManuscriptSse(manuscriptId, input, {
      onEvent: (event, data) => {
        try {
          const obj = JSON.parse(data) as Record<string, unknown>;
          if (event === 'delta') {
            setResult((r) => r + String(obj.text ?? ''));
          } else if (event === 'warnings') {
            setWarnings((obj.items as string[]) ?? []);
          } else if (event === 'done') {
            setPhase('done');
          } else if (event === 'error') {
            setErrorText(String(obj.detail ?? tr('未知错误', 'Unknown error')));
            setPhase('error');
          }
        } catch {
          /* 单个坏帧忽略 */
        }
      },
      onClose: () => {
        // 服务端关流但没收到 done（如网络中断）——已有内容就当完成
        setPhase((p) => (p === 'streaming' ? (resultRef.current ? 'done' : 'error') : p));
      },
      onError: (err) => {
        setErrorText(err instanceof Error ? err.message : String(err));
        setPhase('error');
      },
    });
  }

  function stop() {
    abortRef.current?.();
    abortRef.current = null;
    setPhase(resultRef.current.trim() ? 'done' : 'input');
  }

  function apply(replace: boolean) {
    const text = result.trim();
    if (!text) return;
    if (replace && needsSelection) {
      // 应用前校验选区未被并发编辑动过（CRDT 协同下可能变化）
      const current = view.state.sliceDoc(captured.from, captured.to);
      if (current === captured.text) {
        view.dispatch({
          changes: { from: captured.from, to: captured.to, insert: text },
          selection: { anchor: captured.from + text.length },
          scrollIntoView: true,
        });
        view.focus();
        toast(tr('已替换选中文字', 'Replaced the selection'), 'ok');
        onClose();
        return;
      }
      toast(tr('选中的文字已改动，已改为插入到光标处', 'The selection changed, so the text was inserted at the cursor'), 'info');
    }
    const sel = view.state.selection.main;
    view.dispatch({
      changes: { from: sel.from, to: sel.to, insert: text },
      selection: { anchor: sel.from + text.length },
      scrollIntoView: true,
    });
    view.focus();
    toast(tr('已插入到光标处', 'Inserted at the cursor'), 'ok');
    onClose();
  }

  return (
    <div
      style={{
        borderTop: '0.5px solid var(--border)',
        background: 'var(--surface-2)',
        flexShrink: 0,
        display: 'flex',
        flexDirection: 'column',
        maxHeight: '45%',
      }}
    >
      {/* 头部 */}
      <div className="row gap8" style={{ padding: '7px 14px', flexShrink: 0 }}>
        <Icon name="sparkle" size={13} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: 12, fontWeight: 600 }}>{modeTitle(mode)}</span>
        {needsSelection && !selectionMissing && (
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
            {tr(`已选 ${captured.text.length} 个字符`, `${captured.text.length} characters selected`)}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {phase === 'streaming' && (
          <button className="btn btn-ghost sm" onClick={stop}>
            <Icon name="pause" size={12} />
            {tr('停止', 'Stop')}
          </button>
        )}
        <button className="icon-btn" style={{ width: 24, height: 24 }} onClick={onClose} aria-label={tr('关闭', 'Close')}>
          <Icon name="x" size={13} />
        </button>
      </div>

      {selectionMissing ? (
        <div style={{ padding: '4px 14px 12px', fontSize: 12, color: 'var(--text-3)' }}>
          {mode === 'polish'
            ? tr('先在编辑器中选中要润色的文字。', 'Select the text to polish in the editor first.')
            : tr('先在编辑器中选中要改写的文字。', 'Select the text to rewrite in the editor first.')}
        </div>
      ) : (
        <>
          {/* 输入区：改写要求 / 可选补充 */}
          {(phase === 'input' || phase === 'error') && (
            <div className="row gap8" style={{ padding: '0 14px 10px', flexShrink: 0 }}>
              <input
                className="input"
                style={{ flex: 1, height: 30, fontSize: 12 }}
                placeholder={
                  mode === 'rewrite'
                    ? tr('改写要求，例如：更简洁、语气更正式', 'How to rewrite, e.g. more concise, more formal')
                    : tr('补充要求（可选），按回车开始', 'Extra instructions (optional), press Enter to start')
                }
                value={instruction}
                autoFocus
                onChange={(e) => setInstruction(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') start();
                  if (e.key === 'Escape') onClose();
                }}
              />
              <button className="btn btn-primary sm" onClick={start}>
                <Icon name="sparkle" size={12} />
                {tr('开始', 'Start')}
              </button>
            </div>
          )}

          {phase === 'error' && (
            <div
              style={{
                margin: '0 14px 10px',
                padding: '6px 10px',
                borderRadius: 7,
                background: 'var(--danger-bg)',
                color: 'var(--danger-tx)',
                fontSize: 12,
              }}
            >
              {tr('生成失败：', 'Couldn’t generate: ')}{errorText}
            </div>
          )}

          {/* 结果区 */}
          {(phase === 'streaming' || phase === 'done') && (
            <>
              <div
                ref={resultBoxRef}
                className="scroll mono"
                style={{
                  margin: '0 14px',
                  padding: '8px 10px',
                  borderRadius: 8,
                  border: '0.5px solid var(--border)',
                  background: 'var(--surface)',
                  fontSize: 12,
                  lineHeight: 1.65,
                  whiteSpace: 'pre-wrap',
                  overflowY: 'auto',
                  minHeight: 60,
                  flex: 1,
                }}
              >
                {result || <span style={{ color: 'var(--text-3)' }}>{tr('生成中…', 'Generating…')}</span>}
                {phase === 'streaming' && result && <span className="pulse">▌</span>}
              </div>
              {warnings.length > 0 && (
                <div
                  style={{
                    margin: '8px 14px 0',
                    padding: '6px 10px',
                    borderRadius: 7,
                    background: 'var(--warn-bg)',
                    color: 'var(--warn-tx)',
                    fontSize: 11,
                    lineHeight: 1.6,
                  }}
                >
                  {warnings.map((w, i) => (
                    <div key={i}>{w}</div>
                  ))}
                </div>
              )}
              <div className="row gap8" style={{ padding: '8px 14px 10px', flexShrink: 0 }}>
                {phase === 'done' && (
                  <>
                    {needsSelection && (
                      <button className="btn btn-primary sm" onClick={() => apply(true)}>
                        <Icon name="check" size={12} />
                        {tr('替换选中文字', 'Replace selection')}
                      </button>
                    )}
                    <button
                      className={`btn sm ${needsSelection ? 'btn-soft' : 'btn-primary'}`}
                      onClick={() => apply(false)}
                    >
                      <Icon name="plus" size={12} />
                      {tr('插入到光标处', 'Insert at cursor')}
                    </button>
                    <button
                      className="btn btn-ghost sm"
                      onClick={() => {
                        setPhase('input');
                        setResult('');
                        setWarnings([]);
                      }}
                    >
                      <Icon name="refresh" size={12} />
                      {tr('重新生成', 'Regenerate')}
                    </button>
                  </>
                )}
                <span style={{ flex: 1 }} />
                <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                  {tr('请核对 AI 生成的引用和数字', 'Check citations and numbers before using')}
                </span>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
