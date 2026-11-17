/* ============================================================
   设置 → 模型：只读。

   整个产品固定用一个模型（Claude），不由用户挑；API key 由本机引擎启动时从环境
   变量读取。所以这里没有可填的东西，只回答三件事：现在用的是哪个模型、连得上吗、
   语义搜索那两项（向量、重排）在不在。

   「已连接」只在真检查过一次之后才说：引擎拿到了 key 不等于 key 是对的。
   ============================================================ */
import { Fragment, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from '../../components/ui/Toast';
import { api, type LlmCheckResult, type LlmStatus } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { SettingsGroup, SettingsRow, SettingsSection, SettingsStack, StatusDot, type DotTone } from './settingsUi';

export const LLM_STATUS_KEY = ['llm', 'status'] as const;

/** 文案里用反引号括起来的片段（环境变量名）按等宽代码样式渲染。 */
export function withCode(text: string): ReactNode {
  return text.split('`').map((part, i) =>
    i % 2 === 1 ? (
      <code key={i} className="mono" style={{ fontSize: 'var(--fs-sm)' }}>{part}</code>
    ) : (
      <Fragment key={i}>{part}</Fragment>
    ),
  );
}

/** 没有 key 时怎么办：这是界面上少数几处允许出现环境变量名的地方。 */
export function keySetupText(): string {
  return tr(
    '设置环境变量 `ANTHROPIC_API_KEY`（使用 LiteLLM 网关时设置 `LITELLM_API_KEY` 和 `LITELLM_API_BASE`），然后重新打开 Polaris。',
    'Set the `ANTHROPIC_API_KEY` environment variable (for a LiteLLM gateway, set `LITELLM_API_KEY` and `LITELLM_API_BASE`), then reopen Polaris.',
  );
}

/** 状态点上的那个词。没检查过就不说「已连接」。 */
export function connectionState(
  status: Pick<LlmStatus, 'configured'>,
  check: LlmCheckResult | null,
  checking = false,
): { tone: DotTone; label: string } {
  if (!status.configured) return { tone: 'warn', label: tr('缺少 API key', 'API key missing') };
  if (checking) return { tone: 'busy', label: tr('正在检查…', 'Checking…') };
  if (!check) return { tone: 'idle', label: tr('已设置 API key', 'API key set') };
  return check.ok
    ? { tone: 'ok', label: tr('已连接', 'Connected') }
    : { tone: 'err', label: tr('未连接', 'Not connected') };
}

/** 检查结果 → 一句话。ok 时带耗时；失败时按错误码说清楚是哪一种。 */
export function checkResultText(
  check: LlmCheckResult,
  status: Pick<LlmStatus, 'model' | 'base_host'>,
): string {
  if (check.ok) {
    return check.latency_ms == null
      ? tr('连接正常', 'Connection is working')
      : tr(`连接正常，耗时 ${Math.round(check.latency_ms)} ms`, `Connection is working (${Math.round(check.latency_ms)} ms)`);
  }
  const host = status.base_host || tr('模型服务', 'the model service');
  switch (check.error) {
    case 'not_configured':
      return tr('Polaris 还没有拿到 Claude 的 API key。', 'Polaris has no Claude API key yet.');
    case 'auth_failed':
      return tr('API key 被拒绝，请检查它是否正确。', 'The API key was rejected. Check that it’s correct.');
    case 'unreachable':
      return tr(`无法连接 ${host}，请检查网络。`, `Couldn’t reach ${host}. Check your network.`);
    case 'model_not_found':
      return tr(`${host} 不提供 ${status.model}。`, `${host} doesn’t offer ${status.model}.`);
    case 'rate_limited':
      return tr('请求过于频繁，请稍后再试。', 'Too many requests. Try again in a moment.');
    default:
      return tr('检查没有通过。', 'The check didn’t pass.');
  }
}

function ModelSection({ status }: { status: LlmStatus }) {
  const check = useMutation({ mutationFn: () => api.checkLlmStatus() });
  const result = check.isPending ? null : (check.data ?? null);
  const state = connectionState(status, result, check.isPending);
  const hint = check.isError
    ? errorText(check.error)
    : result
      ? checkResultText(result, status)
      : status.base_host;
  // 兜底错误码没有专门的句子，把服务返回的原话留在第二行
  const detail = result && !result.ok && result.error === 'failed' ? result.detail : null;

  return (
    <SettingsSection title={tr('模型', 'Model')}>
      <SettingsGroup>
        <SettingsRow
          label={
            <>
              Claude · <span className="mono">{status.model}</span>
            </>
          }
          hint={
            <>
              <span role="status">{hint}</span>
              {detail && (
                <span className="mono" style={{ display: 'block', overflowWrap: 'anywhere' }}>{detail}</span>
              )}
            </>
          }
        >
          <StatusDot tone={state.tone}>{state.label}</StatusDot>
          <button
            className="btn btn-ghost sm"
            disabled={check.isPending || !status.configured}
            onClick={() => check.mutate()}
          >
            {tr('检查连接', 'Check connection')}
          </button>
        </SettingsRow>
        {!status.configured && (
          <div className="st-row">
            <span className="st-row-hint">{withCode(keySetupText())}</span>
          </div>
        )}
      </SettingsGroup>
    </SettingsSection>
  );
}

function SearchModelsSection({ status }: { status: LlmStatus }) {
  const rows = [
    {
      key: 'embedding',
      label: tr('语义搜索', 'Semantic search'),
      part: status.embedding,
      missing: tr('不可用，搜索改用关键词匹配', 'Unavailable. Search falls back to keywords.'),
    },
    {
      key: 'rerank',
      label: tr('搜索结果重排', 'Result re-ranking'),
      part: status.rerank,
      missing: tr('不可用，搜索结果不再重排', 'Unavailable. Results aren’t re-ranked.'),
    },
  ];
  return (
    <SettingsSection title={tr('搜索', 'Search')}>
      <SettingsGroup>
        {rows.map((row) => (
          <SettingsRow
            key={row.key}
            label={row.label}
            hint={row.part.available ? <span className="mono">{row.part.model}</span> : row.missing}
          >
            <StatusDot tone={row.part.available ? 'ok' : 'warn'}>
              {row.part.available ? tr('可用', 'Available') : tr('不可用', 'Unavailable')}
            </StatusDot>
          </SettingsRow>
        ))}
      </SettingsGroup>
    </SettingsSection>
  );
}

/**
 * 搜索索引与现在的模型对不上时才出现。
 *
 * 不同模型建出来的索引互相不能比，所以同一时刻只认一份。老安装的索引可能是用别的
 * 模型建的：确认之前新论文一律不建索引（不能把两种混在一起）；确认之后改用新的一份，
 * 已有论文要重新建立索引才搜得到。这不是让用户「换模型」——模型是固定的。
 */
function SearchIndexSection() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ['admin', 'embedding-space'],
    queryFn: () => api.getEmbeddingSpace(),
    retry: false,
  });
  const adopt = useMutation({
    mutationFn: () => api.adoptEmbeddingSpace(),
    onSuccess: () => {
      toast(tr('已换用新的搜索索引', 'Switched to a new search index'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['admin', 'embedding-space'] });
      void queryClient.invalidateQueries({ queryKey: LLM_STATUS_KEY });
    },
    onError: (e) => toast(`${tr('无法重建搜索索引', 'Couldn’t rebuild the search index')}：${errorText(e)}`, 'error'),
  });
  if (!data?.mismatched) return null;
  return (
    <SettingsSection title={tr('搜索索引', 'Search index')}>
      <SettingsGroup>
        <SettingsRow
          label={tr('搜索索引需要重建', 'The search index needs rebuilding')}
          hint={tr(
            '现有索引是用另一个模型建立的，重建前新论文不会建立索引。重建后，已有论文要重新建立索引才能被语义搜索找到。',
            'It was built with a different model, so new papers aren’t indexed until you rebuild. Afterwards, index existing papers again so semantic search finds them.',
          )}
        >
          <button className="btn btn-primary sm" disabled={adopt.isPending} onClick={() => adopt.mutate()}>
            {adopt.isPending ? tr('正在重建…', 'Rebuilding…') : tr('重建索引', 'Rebuild index')}
          </button>
        </SettingsRow>
      </SettingsGroup>
    </SettingsSection>
  );
}

export function ModelSettings() {
  const statusQuery = useQuery({
    queryKey: LLM_STATUS_KEY,
    queryFn: () => api.getLlmStatus(),
    retry: false,
  });
  return (
    <SettingsStack>
      {statusQuery.isLoading ? (
        <SettingsSection title={tr('模型', 'Model')}>
          <SettingsGroup>
            <div className="st-row"><span className="st-row-hint">{tr('加载中…', 'Loading…')}</span></div>
          </SettingsGroup>
        </SettingsSection>
      ) : statusQuery.isError || !statusQuery.data ? (
        <SettingsSection title={tr('模型', 'Model')}>
          <SettingsGroup>
            <SettingsRow label={tr('无法读取模型状态', 'Couldn’t load the model status')}>
              <button className="btn btn-ghost sm" onClick={() => void statusQuery.refetch()}>{tr('重试', 'Retry')}</button>
            </SettingsRow>
          </SettingsGroup>
        </SettingsSection>
      ) : (
        <>
          <ModelSection status={statusQuery.data} />
          <SearchModelsSection status={statusQuery.data} />
        </>
      )}
      <SearchIndexSection />
    </SettingsStack>
  );
}
