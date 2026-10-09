import { Link } from 'react-router-dom';
import type { AssistantBlock } from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';
import { MODEL_SETTINGS_HREF, isLlmNotConfigured, llmNotConfiguredText } from '../../lib/llmNotConfigured';

/* ============================================================
   对话里的出错提示。

   以前把「去设置」写成 Markdown 链接塞进正文，可正文渲染只认 http(s) 链接，
   于是用户看到的是一串 `[去设置](/settings?tab=llm)` 原文。现在出错单独成块，
   落点用站内路由的真链接，点了不刷新页面、正在跑的别的对话也不断。
   ============================================================ */

type NoticeBlock = Extract<AssistantBlock, { kind: 'notice' }>;

/** 后端错误码 → 用户看得懂的一句话。认不出的原样透出，别把线索吃掉。 */
export function errorText(detail: string): string {
  if (detail === 'CHAT_AGENT_DISABLED') {
    return tr(
      '助手已关闭。去掉环境变量 POLARIS_CHAT_AGENT_ENABLED=0 后重新打开 Polaris。',
      'The assistant is turned off. Remove POLARIS_CHAT_AGENT_ENABLED=0 from the environment and restart Polaris.',
    );
  }
  if (isLlmNotConfigured(detail)) return llmNotConfiguredText();
  // 传输层报错带着前缀（"Error: ACP_AGENT_NOT_AVAILABLE"），按包含判断
  if (detail.includes('ACP_AGENT_NOT_AVAILABLE')) {
    return tr(
      '这场对话选的智能体已经不能用了（被删掉或停用）。换一个再问。',
      'The agent picked for this conversation is no longer available (removed or turned off). Pick another one and ask again.',
    );
  }
  return detail;
}

/** 出错 → 提示块。withDetail：把原始细节一并留下，只写「网络错误」等于无从查起。 */
export function errorBlock(detail: string, withDetail = false): NoticeBlock {
  const text = errorText(detail);
  return {
    kind: 'notice',
    text,
    detail: withDetail && detail !== text ? detail : undefined,
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
        color: 'var(--text-2)',
        fontSize: 13,
        lineHeight: 1.6,
      }}
    >
      <span>⚠️ {block.text}</span>
      {block.action === 'model-settings' && (
        <>
          {' '}
          <Link to={MODEL_SETTINGS_HREF} style={{ color: 'var(--accent-text)', fontWeight: 600 }}>
            {tr('去设置', 'Open settings')}
          </Link>
        </>
      )}
      {block.detail && (
        <div className="mono" style={{ marginTop: 4, fontSize: 11.5, color: 'var(--text-4)', overflowWrap: 'anywhere' }}>
          {block.detail}
        </div>
      )}
    </div>
  );
}
