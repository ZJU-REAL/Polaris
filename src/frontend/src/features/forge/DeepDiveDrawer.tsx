import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Drawer } from '../../components/ui/Drawer';
import { Segmented } from '../../components/ui/Segmented';
import { FormField } from '../../components/ui/FormField';
import { Switch } from '../../components/ui/Switch';
import { ConfigRow, RangeControl } from '../voyages/shared/ConfigRow';
import { toast } from '../../components/ui/Toast';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { api, ApiError, type DeepSeedType } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   深度生成抽屉（Idea 2.0，docs/task-system.md §7（原 api-idea2.md §2））：
   种子四选一（自由文本 / 概念 / 论文 / 从草案深化）
   + 生成前人工确认研究目标开关 + 高级选项
   → POST /projects/{pid}/ideas/deep → 跳转任务详情。
   ============================================================ */

/* 文案在渲染处 tr()，避免模块级求值不随语言切换 */
const SEED_TYPES: { v: DeepSeedType; zh: string; en: string }[] = [
  { v: 'text', zh: '描述', en: 'Description' },
  { v: 'concept', zh: '概念', en: 'Concept' },
  { v: 'paper', zh: '论文', en: 'Paper' },
  { v: 'idea', zh: '从草案深化', en: 'From sketch' },
];

export interface DeepDiveDrawerProps {
  open: boolean;
  onClose: () => void;
  pid: string;
  /** 「深化」入口预选的草案（seed.type=idea）。 */
  initialSeedIdea?: { id: string; title: string } | null;
}

export function DeepDiveDrawer({ open, onClose, pid, initialSeedIdea }: DeepDiveDrawerProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [seedType, setSeedType] = useState<DeepSeedType>('text');
  const [seedText, setSeedText] = useState('');
  const [conceptId, setConceptId] = useState('');
  const [conceptQ, setConceptQ] = useState('');
  const [paperId, setPaperId] = useState('');
  const [paperQ, setPaperQ] = useState('');
  const [ideaId, setIdeaId] = useState('');
  // 目标确认闸门默认关（#626）：任务默认直行，想中途拍板的人显式勾选
  const [confirmGoal, setConfirmGoal] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [externalSearch, setExternalSearch] = useState(true);
  const [reviseRounds, setReviseRounds] = useState(2);

  // 打开时重置表单 + 应用「深化」预选
  useEffect(() => {
    if (!open) return;
    setSeedType(initialSeedIdea ? 'idea' : 'text');
    setSeedText('');
    setConceptId('');
    setConceptQ('');
    setPaperId('');
    setPaperQ('');
    setIdeaId(initialSeedIdea?.id ?? '');
    setConfirmGoal(false);
    setShowAdvanced(false);
    setExternalSearch(true);
    setReviseRounds(2);
  }, [open, initialSeedIdea]);

  // —— 种子选项数据 ——
  const conceptsQuery = useQuery({
    queryKey: ['deep-seed-concepts', pid],
    queryFn: () => api.listConcepts(pid),
    enabled: open && seedType === 'concept',
    retry: false,
  });
  const concepts = useMemo(() => {
    const all = conceptsQuery.data ?? [];
    const q = conceptQ.trim().toLowerCase();
    return q ? all.filter((c) => c.name.toLowerCase().includes(q)) : all;
  }, [conceptsQuery.data, conceptQ]);

  const papersQuery = useQuery({
    queryKey: ['deep-seed-papers', pid, paperQ],
    queryFn: () => api.listPapers(pid, { q: paperQ.trim() || undefined, size: 50 }),
    enabled: open && seedType === 'paper',
    retry: false,
  });
  const papers = papersQuery.data?.items ?? [];

  const sketchesQuery = useQuery({
    queryKey: ['deep-seed-sketches', pid],
    queryFn: () => api.listIdeas(pid, { depth: 'sketch', sort: '-created_at' }),
    enabled: open && seedType === 'idea',
    retry: false,
  });
  const sketches = sketchesQuery.data ?? [];

  const seedValue =
    seedType === 'text' ? seedText.trim()
    : seedType === 'concept' ? conceptId
    : seedType === 'paper' ? paperId
    : ideaId;

  const mutation = useMutation({
    mutationFn: () =>
      api.startDeepIdea(pid, {
        seed: { type: seedType, value: seedValue },
        knobs: { confirm_goal: confirmGoal, external_search: externalSearch, revise_rounds: reviseRounds },
      }),
    onSuccess: (v) => {
      toast(tr('已开始深度生成', 'Deep dive started'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['deep-state', pid] });
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      onClose();
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('已有想法任务在运行，请等它完成', 'An idea task is already running. Wait for it to finish.'), 'error');
        void queryClient.invalidateQueries({ queryKey: ['deep-state', pid] });
        void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      } else {
        toast(`${tr('启动失败：', 'Start failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  const canSubmit = !!seedValue && !mutation.isPending;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={
        <>
          <Icon name="sparkle" size={18} style={{ color: 'var(--accent)' }} />
          <span style={{ fontSize: 15, fontWeight: 600 }}>{tr('深度生成', 'Deep dive')}</span>
        </>
      }
      sub={tr('检索文献并起草一份完整的研究方案。', 'Searches the literature and drafts a full proposal.')}
    >
      <FormField label={tr('从哪里开始', 'Start from')}>
        <Segmented<DeepSeedType>
          options={SEED_TYPES.map((s) => ({ v: s.v, label: tr(s.zh, s.en) }))}
          value={seedType}
          onChange={setSeedType}
        />
      </FormField>

      {seedType === 'text' && (
        <FormField label={tr('想法描述', 'Your idea')}>
          <textarea
            className="textarea"
            rows={4}
            value={seedText}
            onChange={(e) => setSeedText(e.target.value)}
            placeholder={tr(
              '例如：能不能用课程学习改进小模型的工具调用能力？',
              'e.g. Could curriculum learning improve tool use in small models?',
            )}
          />
        </FormField>
      )}

      {seedType === 'concept' && (
        <FormField label={tr('概念', 'Concept')}>
          <div className="col gap8">
            <input
              className="input"
              value={conceptQ}
              onChange={(e) => setConceptQ(e.target.value)}
              placeholder={tr('按名称筛选', 'Filter by name')}
            />
            <SelectMenu
              value={conceptId}
              placeholder={conceptsQuery.isLoading ? tr('加载中…', 'Loading…') : conceptsQuery.isError ? tr('无法加载概念', 'Couldn’t load concepts') : concepts.length === 0 ? tr('没有匹配的概念', 'No matching concepts') : tr('选择概念', 'Choose a concept')}
              options={concepts.map((c) => ({
                value: c.id,
                label: `${c.name}${tr(`（${c.paper_count} 篇）`, ` (${c.paper_count === 1 ? '1 paper' : `${c.paper_count} papers`})`)}`,
              }))}
              onChange={setConceptId}
            />
          </div>
        </FormField>
      )}

      {seedType === 'paper' && (
        <FormField label={tr('论文', 'Paper')}>
          <div className="col gap8">
            <input
              className="input"
              value={paperQ}
              onChange={(e) => setPaperQ(e.target.value)}
              placeholder={tr('按关键词搜索', 'Search by keyword')}
            />
            <SelectMenu
              value={paperId}
              placeholder={papersQuery.isLoading ? tr('加载中…', 'Loading…') : papersQuery.isError ? tr('无法加载论文', 'Couldn’t load papers') : papers.length === 0 ? tr('没有匹配的论文', 'No matching papers') : tr('选择论文', 'Choose a paper')}
              options={papers.map((p) => ({
                value: p.id,
                label: `${p.title}${p.year ? tr(`（${p.year}）`, ` (${p.year})`) : ''}`,
              }))}
              onChange={setPaperId}
            />
          </div>
        </FormField>
      )}

      {seedType === 'idea' && (
        <FormField label={tr('草案', 'Sketch')} hint={tr('沿用草案引用的论文', 'Reuses the papers the sketch cites')}>
          <SelectMenu
            value={ideaId}
            placeholder={sketchesQuery.isLoading ? tr('加载中…', 'Loading…') : sketchesQuery.isError ? tr('无法加载草案', 'Couldn’t load sketches') : sketches.length === 0 ? tr('还没有草案，请先生成想法', 'No sketches yet. Generate ideas first') : tr('选择草案', 'Choose a sketch')}
            options={[
              ...(initialSeedIdea && !sketches.some((s) => s.id === initialSeedIdea.id)
                ? [{ value: initialSeedIdea.id, label: initialSeedIdea.title }]
                : []),
              ...sketches.map((s) => ({ value: s.id, label: s.title })),
            ]}
            onChange={setIdeaId}
          />
        </FormField>
      )}

      <div className="settings-list" style={{ marginBottom: 8 }}>
        <ConfigRow label={tr('先确认研究目标', 'Approve the research goal first')} hint={tr('确认后再起草方案', 'Drafting starts after you approve')}>
          <Switch checked={confirmGoal} onChange={setConfirmGoal} aria-label={tr('先确认研究目标', 'Approve the research goal first')} />
        </ConfigRow>
      </div>

      <button
        type="button"
        className="btn btn-ghost sm"
        style={{ marginBottom: showAdvanced ? 10 : 14, paddingLeft: 0 }}
        onClick={() => setShowAdvanced((v) => !v)}
      >
        <Icon name={showAdvanced ? 'chevDown' : 'chevron'} size={13} />
        {tr('高级选项', 'Advanced')}
      </button>
      {showAdvanced && (
        <div className="settings-list" style={{ marginBottom: 14 }}>
          <ConfigRow label={tr('站外查找相似工作', 'Search for similar work online')} hint={tr('Semantic Scholar 和 OpenAlex', 'Semantic Scholar and OpenAlex')}>
            <Switch checked={externalSearch} onChange={setExternalSearch} aria-label={tr('站外查找相似工作', 'Search for similar work online')} />
          </ConfigRow>
          <ConfigRow label={tr('修订轮数', 'Revision rounds')} hint={tr('评审并修改方案的最多轮数', 'Most rounds of review and revision')}>
            <RangeControl ariaLabel={tr('修订轮数', 'Revision rounds')} value={reviseRounds} min={0} max={4} step={1} onChange={setReviseRounds} />
          </ConfigRow>
        </div>
      )}

      <button
        className="btn btn-primary"
        style={{ width: '100%', justifyContent: 'center', marginTop: 6 }}
        disabled={!canSubmit}
        onClick={() => mutation.mutate()}
      >
        {mutation.isPending ? (
          <>
            <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
            {tr('启动中…', 'Starting…')}
          </>
        ) : (
          <>
            <Icon name="play" size={14} />
            {tr('开始深度生成', 'Start deep dive')}
          </>
        )}
      </button>
    </Drawer>
  );
}
