import { Link } from 'react-router-dom';
import type { AssistantBlock } from '../../lib/assistantStream';
import { tr } from '../../lib/i18n';
import { MODEL_SETTINGS_HREF, isLlmNotConfigured, llmNotConfiguredText } from '../../lib/llmNotConfigured';
import { errorText as genericErrorText } from '../../lib/errors';
import { LOCAL_AGENTS_HREF } from './backendChoice';

/* ============================================================
   对话里的出错提示。

   以前把「去设置」写成 Markdown 链接塞进正文，可正文渲染只认 http(s) 链接，
   于是用户看到的是一串 `[去设置](/settings?tab=…)` 原文。现在出错单独成块，
   落点用站内路由的真链接，点了不刷新页面、正在跑的别的对话也不断。
   ============================================================ */

type NoticeBlock = Extract<AssistantBlock, { kind: 'notice' }>;

/** 本机智能体出错：错误码 → 一句话，说清是哪一种、该做什么。认不出的返回 null。 */
export function agentErrorText(code: string | undefined, detail = ''): string | null {
  // 传输层报错带着前缀（"Error: ACP_AGENT_NOT_AVAILABLE"），按包含判断
  if (code === 'ACP_AGENT_NOT_AVAILABLE' || detail.includes('ACP_AGENT_NOT_AVAILABLE')) {
    return tr(
      '这段对话选的智能体已被移除或停用。换一个回答方后再发一次。',
      'The agent picked for this conversation was removed or turned off. Pick another one and send again.',
    );
  }
  switch (code) {
    case 'ACP_AGENT_NOT_INSTALLED':
      return tr(
        '本机找不到这个智能体。安装后在「本机智能体」里检查连接，再发一次。',
        'This agent isn’t installed on this computer. Install it, check the connection in Local agents, then send again.',
      );
    case 'ACP_AGENT_NEEDS_LOGIN':
      return tr(
        '这个智能体还没有可用的凭据。在终端里运行它的登录命令，然后再发一次。',
        'This agent isn’t signed in. Run its sign-in command in a terminal, then send again.',
      );
    case 'ACP_AGENT_TIMEOUT':
      return tr('智能体太久没有回应，请再发一次。', 'The agent took too long to respond. Send again.');
    case 'ACP_AGENT_CRASHED':
      return tr('智能体意外退出了，再发一次会重新启动它。', 'The agent quit unexpectedly. Send again to restart it.');
    case 'ACP_AGENT_FAILED':
      return tr('智能体没能完成这次回答。', 'The agent couldn’t finish this reply.');
    default:
      return null;
  }
}

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

/** 出错 → 提示块。withDetail：把原始细节一并留下，只写「网络错误」等于无从查起。
    code：后端错误帧带的码；智能体出错时据此说清是哪一种。 */
export function errorBlock(detail: string, withDetail = false, code?: string): NoticeBlock {
  const agentText = agentErrorText(code, detail);
  if (agentText) {
    const fixable = code === 'ACP_AGENT_NOT_INSTALLED' || code === 'ACP_AGENT_NEEDS_LOGIN' || !code;
    return {
      kind: 'notice',
      text: agentText,
      detail: detail && !detail.includes('ACP_AGENT_NOT_AVAILABLE') ? detail : undefined,
      action: fixable ? 'agent-settings' : undefined,
    };
  }
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
      {block.action === 'agent-settings' && (
        <>
          {' '}
          <Link to={LOCAL_AGENTS_HREF} style={{ color: 'var(--accent-text)', fontWeight: 500 }}>
            {tr('打开本机智能体', 'Open Local agents')}
          </Link>
        </>
      )}
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
