import { tr } from './i18n';

/* 「没有可用的模型」这一类错误（#840）：接口回的错误码、流里透出的异常名或后端原话，
   都认成同一件事，统一给一句能照着做的大白话。 */

export const MODEL_SETTINGS_HREF = '/settings?tab=llm';

export function isLlmNotConfigured(detail: string): boolean {
  return (
    detail.includes('LLM_NOT_CONFIGURED') ||
    detail.includes('LLMNotConfiguredError') ||
    detail.toLowerCase().includes('no model configured')
  );
}

export function llmNotConfiguredText(): string {
  return tr(
    '还没有可用的模型，请在「设置 → 模型与智能体」中添加智能体或模型服务。',
    'No model is available. Add an agent or a model provider in Settings → Models & agents.',
  );
}
