import { tr } from './i18n';

/* 「引擎没有拿到 Claude 的 API key」这一类错误：接口回的错误码、流里透出的异常名或
   后端原话，都认成同一件事，统一给一句能照着做的大白话。

   模型是固定的，key 由本机引擎启动时从环境变量读取——所以这句话只能教人设环境变量，
   这是界面上少数几处允许出现环境变量名的地方。 */

export const MODEL_SETTINGS_HREF = '/settings?tab=model';

export function isLlmNotConfigured(detail: string): boolean {
  return (
    detail.includes('LLM_NOT_CONFIGURED') ||
    detail.includes('LLMNotConfiguredError') ||
    detail.toLowerCase().includes('no model configured')
  );
}

export function llmNotConfiguredText(): string {
  return tr(
    'Polaris 还没有拿到 Claude 的 API key。设置环境变量 ANTHROPIC_API_KEY 后重新打开 Polaris。',
    'Polaris has no Claude API key yet. Set the ANTHROPIC_API_KEY environment variable, then reopen Polaris.',
  );
}
