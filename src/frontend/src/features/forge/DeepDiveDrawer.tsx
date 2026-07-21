import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Drawer } from '../../components/ui/Drawer';
import { Segmented } from '../../components/ui/Segmented';
import { FormField } from '../../components/ui/FormField';
import { KnobRange } from '../../components/ui/KnobRange';
import { toast } from '../../components/ui/Toast';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { api, ApiError, type DeepSeedType } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   深度生成抽屉（Idea 2.0，docs/api-idea2.md §2）：
   种子四选一（自由文本 / 概念 / 论文 / 从草案深化）
   + 「生成前人工确认研究目标」开关 + 高级选项
   → POST /projects/{pid}/ideas/deep → 跳转任务详情。
   ============================================================ */

/* 文案在渲染处 tr()，避免模块级求值不随语言切换 */
const SEED_TYPES: { v: DeepSeedType; zh: string; en: string }[] = [
  { v: 'text', zh: '自由文本', en: 'Free text' },
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
  const [confirmGoal, setConfirmGoal] = useState(true);
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
    setConfirmGoal(true);
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
      toast(tr('深度生成已开始，跳转任务详情…', 'Deep Dive started — opening the task…'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['deep-state', pid] });
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      onClose();
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('已有 AI 想法任务在运行，请等待其完成。', 'An AI idea task is already running — wait for it to finish.'), 'error');
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
          <span style={{ fontSize: 15, fontWeight: 680 }}>{tr('深度生成', 'Deep Dive')}</span>
        </>
      }
      sub={tr('AI 检索文献并起草完整研究方案，研究目标可在生成前人工确认。', 'The AI searches the literature and drafts a full proposal; the research goal can be confirmed by you first.')}
    >
      <FormField
        label={tr('种子', 'Seed')}
        en="seed"
        hint={tr(
          '给 AI 一个探索起点：一段想法、一个概念、一篇论文，或把已有草案深化成完整研究方案。',
          'Give the AI a starting point: a thought, a concept, a paper, or deepen an existing sketch into a full proposal.',
        )}
      >
        <Segmented<DeepSeedType>
          options={SEED_TYPES.map((s) => ({ v: s.v, label: tr(s.zh, s.en) }))}
          value={seedType}
          onChange={setSeedType}
        />
      </FormField>

      {seedType === 'text' && (
        <FormField label={tr('想法描述', 'Describe your idea')} en="free text">
          <textarea
            className="textarea"
            rows={4}
            value={seedText}
            onChange={(e) => setSeedText(e.target.value)}
            placeholder={tr(
              '描述你想探索的研究方向、问题或直觉，比如：能不能用课程学习改进小模型的工具调用能力？',
              'Describe the direction, question or hunch you want to explore, e.g. could curriculum learning improve tool use in small models?',
            )}
          />
        </FormField>
      )}

      {seedType === 'concept' && (
        <FormField label={tr('选择概念', 'Pick a concept')} en="concept" hint={tr('从当前方向的概念库中选一个作为探索起点。', 'Pick one from this direction’s concept library as the starting point.')}>
          <div className="col gap8">
            <input
              className="input"
              value={conceptQ}
              onChange={(e) => setConceptQ(e.target.value)}
              placeholder={tr('输入名称过滤概念…', 'Type a name to filter concepts…')}
            />
            <SelectMenu
              value={conceptId}
              placeholder={conceptsQuery.isLoading ? tr('加载中…', 'Loading…') : conceptsQuery.isError ? tr('（无法加载概念列表）', '(could not load concepts)') : concepts.length === 0 ? tr('（没有匹配的概念）', '(no matching concepts)') : tr('— 选择概念 —', '— pick a concept —')}
              options={concepts.map((c) => ({
                value: c.id,
                label: `${c.name}${tr(`（${c.paper_count} 篇）`, ` (${c.paper_count} papers)`)}`,
              }))}
              onChange={setConceptId}
            />
          </div>
        </FormField>
      )}

      {seedType === 'paper' && (
        <FormField label={tr('选择论文', 'Pick a paper')} en="paper" hint={tr('从文献库中选一篇作为探索起点。', 'Pick one from the library as the starting point.')}>
          <div className="col gap8">
            <input
              className="input"
              value={paperQ}
              onChange={(e) => setPaperQ(e.target.value)}
              placeholder={tr('输入关键词搜索论文…', 'Type keywords to search papers…')}
            />
            <SelectMenu
              value={paperId}
              placeholder={papersQuery.isLoading ? tr('加载中…', 'Loading…') : papersQuery.isError ? tr('（无法加载论文列表）', '(could not load papers)') : papers.length === 0 ? tr('（没有匹配的论文）', '(no matching papers)') : tr('— 选择论文 —', '— pick a paper —')}
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
        <FormField label={tr('选择草案', 'Pick a sketch')} en="seed idea" hint={tr('把一份方向草案深化为完整研究方案，AI 会继承草案的依据文献继续探索。', 'Deepen a sketch into a full proposal; the AI inherits its evidence papers and keeps exploring.')}>
          <SelectMenu
            value={ideaId}
            placeholder={sketchesQuery.isLoading ? tr('加载中…', 'Loading…') : sketchesQuery.isError ? tr('（无法加载草案列表）', '(could not load sketches)') : sketches.length === 0 ? tr('（还没有草案，先运行一次想法生成）', '(no sketches yet — run idea generation first)') : tr('— 选择草案 —', '— pick a sketch —')}
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

      <FormField
        label={tr('生成前人工确认研究目标', 'Confirm the research goal first')}
        en="confirm_goal"
        hint={tr(
          'AI 构建好研究目标后先暂停等你确认（可附修改意见），确认后才继续起草方案。关闭则全程自动。',
          'The AI pauses after building the research goal and waits for your confirmation (with optional feedback) before drafting. Turn off for fully automatic.',
        )}
      >
        <label className="row gap8" style={{ fontSize: 12.5, cursor: 'pointer' }}>
          <input type="checkbox" checked={confirmGoal} onChange={(e) => setConfirmGoal(e.target.checked)} />
          {tr('开启（推荐）', 'On (recommended)')}
        </label>
      </FormField>

      <button
        type="button"
        className="btn btn-ghost sm"
        style={{ marginBottom: showAdvanced ? 10 : 14, paddingLeft: 0 }}
        onClick={() => setShowAdvanced((v) => !v)}
      >
        <Icon name={showAdvanced ? 'chevDown' : 'chevron'} size={13} />
        {tr('高级选项', 'Advanced options')}
      </button>
      {showAdvanced && (
        <>
          <FormField
            label={tr('外部相似检索', 'External similarity search')}
            en="external_search"
            hint={tr(
              '联网检索 Semantic Scholar / OpenAlex 做相似工作对比，新颖性核查更可靠；关闭则只查库内。',
              'Searches Semantic Scholar / OpenAlex for similar work, making the novelty check more reliable; off means library-only.',
            )}
          >
            <label className="row gap8" style={{ fontSize: 12.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={externalSearch} onChange={(e) => setExternalSearch(e.target.checked)} />
              {tr('开启外部检索', 'Enable external search')}
            </label>
          </FormField>
          <KnobRange
            label={tr('评审修订轮数', 'Review & revise rounds')}
            en="revise_rounds"
            hint={tr('自动评审与修订的最大轮数。', 'Max rounds of automatic review and revision.')}
            value={reviseRounds}
            min={0}
            max={4}
            step={1}
            onChange={setReviseRounds}
          />
        </>
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
            {tr('开始深度生成', 'Start Deep Dive')}
          </>
        )}
      </button>
      <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6, marginTop: 12 }}>
        {tr(
          '全过程：目标构建（AI 带文献工具探索）→ 目标确认（人工）→ 方案起草（相关工作 / 设计 / 实验计划 / 新颖性核查 / 风险）→ 多评审员评审与修订 → 入候选池。进度可在任务详情页实时查看。',
          'Full flow: goal building (AI explores with literature tools) → goal confirmation (human) → proposal drafting (related work / design / experiment plan / novelty check / risks) → multi-reviewer review & revision → into the candidate pool. Watch progress live on the task page.',
        )}
      </div>
    </Drawer>
  );
}
