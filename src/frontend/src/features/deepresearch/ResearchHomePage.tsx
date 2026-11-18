import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { api, ApiError } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { isLlmNotConfigured, llmNotConfiguredText, MODEL_SETTINGS_HREF } from '../../lib/llmNotConfigured';
import { isEngineUnreachable, researchApi, type ResearchBrief, type ResearchRunRead } from '../../lib/researchApi';
import { StatusDot } from '../voyages/shared/StatusDot';
import { BriefEditor } from './BriefEditor';
import { DirectionBox, loadDraft, saveDraft } from './DirectionBox';
import { researchKeys, useResearchRuns } from './hooks';
import { countsText, runHeadline, runReason, waitingFor } from './phase';
import './deepresearch.css';

/* ============================================================
   /research —— 打开应用的第一站。
   一个输入框（想研究什么）+ 已有课题的列表（各自走到哪、有没有在等你）。
   默认路径只有一步：写方向 → 开始。「调整范围」是可选的，会先让模型拟一份
   检索范围给你改。
   ============================================================ */

/** 等用户的排最前，其次是运行中的，再是其余；同组内按最近更新。 */
function sortRuns(runs: ResearchRunRead[]): ResearchRunRead[] {
  const rank = (r: ResearchRunRead) => (waitingFor(r) ? 0 : r.status === 'running' ? 1 : 2);
  return [...runs].sort((a, b) => rank(a) - rank(b) || b.updated_at.localeCompare(a.updated_at));
}

/** 首次打开时给的几个方向例子（跨几个学科，都是一句话能说清的方向）。 */
function exampleDirections(): string[] {
  return [
    tr('用扩散模型设计蛋白质骨架', 'Diffusion models for protein backbone design'),
    tr('检索增强生成中检索器与生成器的联合训练', 'Joint training of retriever and generator in retrieval-augmented generation'),
    tr('大模型智能体的长期记忆机制', 'Long-term memory for LLM agents'),
    tr('钙钛矿太阳能电池的界面钝化', 'Interface passivation in perovskite solar cells'),
  ];
}

/**
 * 开始 / 拟范围失败时框下面那句话。模型这一侧的失败（接口回 424，detail 是
 * LLM_AUTH / LLM_UNREACHABLE / PLAN_FAILED，或者引擎根本没有 key）多给一个去看模型状态的链接。
 */
function describeError(e: unknown): { text: string; needsModel: boolean } {
  const code = e instanceof ApiError ? e.message : '';
  const notConfigured = isLlmNotConfigured(code);
  return {
    text: notConfigured ? llmNotConfiguredText() : errorText(e),
    needsModel: notConfigured || code === 'LLM_AUTH' || (e instanceof ApiError && e.status === 424),
  };
}

function newClientKey(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
}

export function ResearchHomePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [direction, setDirection] = useState(loadDraft);
  const [brief, setBrief] = useState<ResearchBrief | null>(null);
  const [formError, setFormErrorState] = useState<{ text: string; needsModel: boolean } | null>(null);
  const setFormError = (e: unknown) => setFormErrorState(e === null ? null : describeError(e));
  const boxRef = useRef<HTMLTextAreaElement | null>(null);
  // 同一段文字重复点「开始」用同一个幂等键：第一次其实成功了但回应丢了，也不会建出两个课题
  const keyRef = useRef<{ text: string; key: string } | null>(null);

  useEffect(() => {
    saveDraft(direction);
  }, [direction]);

  const runsQuery = useResearchRuns();
  const runs = useMemo(() => sortRuns(runsQuery.data ?? []), [runsQuery.data]);
  const engineDown = runsQuery.isError && isEngineUnreachable(runsQuery.error);

  // 模型没连上时研究开始不了：提前说，而不是等点了才报错
  const llmQuery = useQuery({
    queryKey: ['llm-status'],
    queryFn: () => api.getLlmStatus(),
    retry: false,
    staleTime: 30_000,
  });
  const modelMissing = llmQuery.data?.configured === false;

  const create = useMutation({
    mutationFn: (withBrief: ResearchBrief | undefined) => {
      const text = direction.trim();
      if (!keyRef.current || keyRef.current.text !== text) keyRef.current = { text, key: newClientKey() };
      return researchApi.createRun({ direction: text, client_key: keyRef.current.key, brief: withBrief });
    },
    onSuccess: (run) => {
      saveDraft('');
      keyRef.current = null;
      void queryClient.invalidateQueries({ queryKey: researchKeys.list });
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast(tr('已开始研究', 'Research started'), 'ok');
      navigate(`/research/${run.id}`);
    },
    onError: (e) => setFormError(e),
  });

  const draft = useMutation({
    mutationFn: () => researchApi.draftBrief(direction.trim()),
    onSuccess: (b) => setBrief(b),
    onError: (e) => setFormError(e),
  });

  function start(withBrief?: ResearchBrief) {
    setFormError(null);
    create.mutate(withBrief);
  }

  const hasRuns = runs.length > 0;
  // 第一次打开（还没有课题、框是空的）：给几个方向做例子，点一下填进框里
  const showExamples = !hasRuns && !runsQuery.isLoading && !brief && direction.trim() === '' && !draft.isPending;

  return (
    <div className={'page fadeup dr-home' + (hasRuns ? ' has-runs' : '')}>
      <h1 className="dr-home-title">{tr('想研究什么？', 'What do you want to research?')}</h1>
      <p className="dr-home-sub">
        {tr(
          '写下一个方向。Polaris 会检索文献、寻找有文献依据的假设，再把想法列出来给你选。',
          'Describe a direction. Polaris surveys the literature, looks for hypotheses the papers support, and lists the ideas for you to pick from.',
        )}
      </p>

      {/* 没有 Claude 的 key 什么都开始不了：在框的上方说清楚要做什么，而不是等点了才报错 */}
      {modelMissing && (
        <div className="dr-setup" role="status">
          <div className="dr-setup-text">
            <div className="dr-setup-title">{tr('先连接 Claude', 'Connect Claude first')}</div>
            <div className="dr-setup-body">{llmNotConfiguredText()}</div>
          </div>
          <Link className="btn btn-ghost sm" to={MODEL_SETTINGS_HREF}>
            {tr('查看模型状态', 'Check model status')}
          </Link>
        </div>
      )}

      <DirectionBox
        value={direction}
        inputRef={boxRef}
        onChange={(v) => {
          setDirection(v);
          if (formError) setFormError(null);
        }}
        onSubmit={() => start()}
        onAdjust={() => {
          setFormError(null);
          draft.mutate();
        }}
        submitting={create.isPending && !brief}
        drafting={draft.isPending}
        adjusting={!!brief}
        blocked={modelMissing}
      />

      {/* 框下面至多一行提示：按紧要程度只显示一条 */}
      {formError ? (
        <div className="dr-note is-error" role="alert">
          {formError.text}
          {formError.needsModel && (
            <>
              {' '}
              <Link to={MODEL_SETTINGS_HREF}>{tr('查看模型状态', 'Check model status')}</Link>
            </>
          )}
        </div>
      ) : engineDown ? (
        <div className="dr-note is-warn" role="status">
          {tr('无法连接本机引擎，正在重试…', 'Couldn’t reach the local engine. Retrying…')}
        </div>
      ) : draft.isPending ? (
        <div className="dr-note" role="status">
          {tr('正在根据你的方向拟一份检索范围，通常需要十几秒。', 'Drafting a search scope from your direction. This usually takes ten seconds or so.')}
        </div>
      ) : null}

      {brief && (
        <BriefEditor
          brief={brief}
          submitLabel={tr('开始研究', 'Start research')}
          busyLabel={tr('正在开始…', 'Starting…')}
          busy={create.isPending}
          onSubmit={(b) => start(b)}
          onCancel={() => {
            setBrief(null);
            boxRef.current?.focus();
          }}
        />
      )}

      {showExamples && (
        <div className="dr-examples">
          <div className="dr-examples-label">{tr('可以从这些方向试起', 'Or start from one of these')}</div>
          <div className="dr-examples-list">
            {exampleDirections().map((text) => (
              <button
                key={text}
                type="button"
                className="dr-example"
                onClick={() => {
                  setDirection(text);
                  setFormError(null);
                  // 填进去之后光标回到框里，接着改或者直接回车
                  requestAnimationFrame(() => {
                    const el = boxRef.current;
                    if (!el) return;
                    el.focus();
                    el.setSelectionRange(text.length, text.length);
                  });
                }}
              >
                {text}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 还没有课题时不画这一段：一个只写着「还没有课题」的空列表帮不上第一次来的人 */}
      {(runsQuery.isLoading || runsQuery.isError || hasRuns) && (
      <div className="dr-list">
        <div className="group-label">{tr('课题', 'Topics')}</div>
        {runsQuery.isLoading ? (
          <LoadingState rows={3} />
        ) : runsQuery.isError && !runsQuery.data ? (
          engineDown ? (
            // 连不上已经在框下面说过了，这里不再重复一遍
            <div className="dr-list-empty">{tr('连接后会显示你的课题', 'Your topics appear here once connected')}</div>
          ) : (
            <ErrorState
              compact
              title={tr('无法加载课题，请重试', 'Couldn’t load your topics. Try again.')}
              detail={errorText(runsQuery.error)}
              onRetry={() => void runsQuery.refetch()}
            />
          )
        ) : (
          <div className="dr-rows">
            {runs.map((run) => (
              <RunRow key={run.id} run={run} />
            ))}
          </div>
        )}
      </div>
      )}
    </div>
  );
}

function RunRow({ run }: { run: ResearchRunRead }) {
  const head = runHeadline(run);
  const reason = runReason(run);
  const meta = [countsText(run.counts), fmtRelative(run.updated_at)].filter(Boolean).join(' · ');
  return (
    <Link to={`/research/${run.id}`} className="dr-row">
      <div className="dr-row-name" title={run.name}>
        {run.name}
      </div>
      <div className="dr-row-state">
        <StatusDot tone={head.tone} label={head.label} />
        {reason && (
          <span className="dr-row-reason" title={reason}>
            {reason}
          </span>
        )}
        <span className="dr-row-meta">{meta}</span>
      </div>
    </Link>
  );
}
