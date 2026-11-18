import { Link } from 'react-router-dom';
import type { AssistantBlock } from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';
import { MODEL_SETTINGS_HREF, isLlmNotConfigured, llmNotConfiguredText } from '../../lib/llmNotConfigured';
import { errorText as genericErrorText } from '../../lib/errors';

/* ============================================================
   对话里的出错提示。

   以前把「去设置」写成 Markdown 链接塞进正文，可正文渲染只认 http(s) 链接，
   于是用户看到的是一串 `[去设置](/settings?tab=…)` 原文。现在出错单独成块，
   落点用站内路由的真链接，点了不刷新页面、正在跑的别的对话也不断。
   ============================================================ */

type NoticeBlock = Extract<AssistantBlock, { kind: 'notice' }>;

/** 后端错误码 → 用户看得懂的一句话。认不出的走 lib/errors 的通用处理（原码留在括号里，别把线索吃掉）。 */
export function errorText(detail: string): string {
  if (detail === 'CHAT_AGENT_DISABLED') {
    return tr(
      '助手已关闭。去掉环境变量 POLARIS_CHAT_AGENT_ENABLED=0 后重新打开 Polaris。',
      'The assistant is turned off. Remove POLARIS_CHAT_AGENT_ENABLED=0 from the environment and restart Polaris.',
    );
  }
  if (isLlmNotConfigured(detail)) return llmNotConfiguredText();
  // 模型的回答传到一半断了（引擎原话是「LLMTransportError: model stream ended before the final message」）：
  // 异常类名对用户没有意义，说清发生了什么、怎么办
  if (/LLMTransportError|stream ended before|incomplete chunked|peer closed connection/i.test(detail)) {
    return tr('模型的回答中途断开了，请再发一次', 'The model’s reply was cut off before it finished. Send your message again.');
  }
  if (/LLMTimeout|timed? ?out/i.test(detail) && /LLM|model/i.test(detail)) {
    return tr('模型太久没有回应，请再发一次', 'The model took too long to respond. Send your message again.');
  }
  return genericErrorText(detail);
}

/** 出错 → 提示块。withDetail：把原始细节一并留下，只写「网络错误」等于无从查起。 */
export function errorBlock(detail: string, withDetail = false): NoticeBlock {
  const text = errorText(detail);
  return {
    kind: 'notice',
    text,
    // 换成了人话的，原话留在第二行给排查用
    detail: (withDetail || /Error:/.test(detail)) && detail !== text ? detail : undefined,
    action: isLlmNotConfigured(detail) ? 'model-settings' : undefined,
  };
}

export function NoticeView({ block }: { block: NoticeBlock }) {
  return (
    <div
      role="alert"
      style={{
        padding: '8px 10px',
        borderRadius: 8,
        background: 'var(--surface-2)',
        borderLeft: '2px solid var(--warn)',
        color: 'var(--text-2)',
        fontSize: 13,
        lineHeight: 1.6,
      }}
    >
      <span>{block.text}</span>
      {block.action === 'model-settings' && (
        <>
          {' '}
          <Link to={MODEL_SETTINGS_HREF} style={{ color: 'var(--accent-text)', fontWeight: 600 }}>
            {tr('查看模型状态', 'View model status')}
          </Link>
        </>
      )}
      {block.detail && (
        <div className="mono" style={{ marginTop: 4, fontSize: 12, color: 'var(--text-3)', overflowWrap: 'anywhere' }}>
          {block.detail}
        </div>
      )}
    </div>
  );
}
