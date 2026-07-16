import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Drawer } from '../../components/ui/Drawer';
import { Segmented } from '../../components/ui/Segmented';
import { FormField } from '../../components/ui/FormField';
import { KnobRange } from '../../components/ui/KnobRange';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type DeepSeedType } from '../../lib/api';

/* ============================================================
   深度生成抽屉（Idea 2.0，docs/api-idea2.md §2）：
   种子四选一（自由文本 / 概念 / 论文 / 从草案深化）
   + 「生成前人工确认研究目标」开关 + 高级选项
   → POST /projects/{pid}/ideas/deep → 跳转任务详情。
   ============================================================ */

const SEED_TYPES: { v: DeepSeedType; label: string }[] = [
  { v: 'text', label: '自由文本' },
  { v: 'concept', label: '概念' },
  { v: 'paper', label: '论文' },
  { v: 'idea', label: '从草案深化' },
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
      toast('深度生成已开始，跳转任务详情…', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['deep-state', pid] });
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      onClose();
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast('已有 AI 想法任务在运行，请等待其完成。', 'error');
        void queryClient.invalidateQueries({ queryKey: ['deep-state', pid] });
        void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      } else {
        toast(`启动失败：${e instanceof Error ? e.message : String(e)}`, 'error');
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
          <span style={{ fontSize: 15, fontWeight: 680 }}>深度生成</span>
          <span className="en-label" style={{ fontSize: 11 }}>Deep Dive</span>
        </>
      }
      sub="AI 检索文献并起草完整研究方案，研究目标可在生成前人工确认。"
    >
      <FormField label="种子" en="seed" hint="给 AI 一个探索起点：一段想法、一个概念、一篇论文，或把已有草案深化成完整研究方案。">
        <Segmented<DeepSeedType> options={SEED_TYPES} value={seedType} onChange={setSeedType} />
      </FormField>

      {seedType === 'text' && (
        <FormField label="想法描述" en="free text">
          <textarea
            className="textarea"
            rows={4}
            value={seedText}
            onChange={(e) => setSeedText(e.target.value)}
            placeholder="描述你想探索的研究方向、问题或直觉，比如：能不能用课程学习改进小模型的工具调用能力？"
          />
        </FormField>
      )}

      {seedType === 'concept' && (
        <FormField label="选择概念" en="concept" hint="从当前方向的概念库中选一个作为探索起点。">
          <div className="col gap8">
            <input
              className="input"
              value={conceptQ}
              onChange={(e) => setConceptQ(e.target.value)}
              placeholder="输入名称过滤概念…"
            />
            <select className="input" value={conceptId} onChange={(e) => setConceptId(e.target.value)}>
              <option value="" disabled>
                {conceptsQuery.isLoading ? '加载中…' : conceptsQuery.isError ? '（无法加载概念列表）' : concepts.length === 0 ? '（没有匹配的概念）' : '— 选择概念 —'}
              </option>
              {concepts.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}（{c.paper_count} 篇）
                </option>
              ))}
            </select>
          </div>
        </FormField>
      )}

      {seedType === 'paper' && (
        <FormField label="选择论文" en="paper" hint="从文献库中选一篇作为探索起点。">
          <div className="col gap8">
            <input
              className="input"
              value={paperQ}
              onChange={(e) => setPaperQ(e.target.value)}
              placeholder="输入关键词搜索论文…"
            />
            <select className="input" value={paperId} onChange={(e) => setPaperId(e.target.value)}>
              <option value="" disabled>
                {papersQuery.isLoading ? '加载中…' : papersQuery.isError ? '（无法加载论文列表）' : papers.length === 0 ? '（没有匹配的论文）' : '— 选择论文 —'}
              </option>
              {papers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}{p.year ? `（${p.year}）` : ''}
                </option>
              ))}
            </select>
          </div>
        </FormField>
      )}

      {seedType === 'idea' && (
        <FormField label="选择草案" en="seed idea" hint="把一份方向草案深化为完整研究方案，AI 会继承草案的依据文献继续探索。">
          <select className="input" value={ideaId} onChange={(e) => setIdeaId(e.target.value)}>
            <option value="" disabled>
              {sketchesQuery.isLoading ? '加载中…' : sketchesQuery.isError ? '（无法加载草案列表）' : sketches.length === 0 ? '（还没有草案，先运行一次想法生成）' : '— 选择草案 —'}
            </option>
            {initialSeedIdea && !sketches.some((s) => s.id === initialSeedIdea.id) && (
              <option value={initialSeedIdea.id}>{initialSeedIdea.title}</option>
            )}
            {sketches.map((s) => (
              <option key={s.id} value={s.id}>{s.title}</option>
            ))}
          </select>
        </FormField>
      )}

      <FormField
        label="生成前人工确认研究目标"
        en="confirm_goal"
        hint="AI 构建好研究目标后先暂停等你确认（可附修改意见），确认后才继续起草方案。关闭则全程自动。"
      >
        <label className="row gap8" style={{ fontSize: 12.5, cursor: 'pointer' }}>
          <input type="checkbox" checked={confirmGoal} onChange={(e) => setConfirmGoal(e.target.checked)} />
          开启（推荐）
        </label>
      </FormField>

      <button
        type="button"
        className="btn btn-ghost sm"
        style={{ marginBottom: showAdvanced ? 10 : 14, paddingLeft: 0 }}
        onClick={() => setShowAdvanced((v) => !v)}
      >
        <Icon name={showAdvanced ? 'chevDown' : 'chevron'} size={13} />
        高级选项 <span style={{ color: 'var(--text-4)', fontSize: 11 }}>advanced</span>
      </button>
      {showAdvanced && (
        <>
          <FormField
            label="外部相似检索"
            en="external_search"
            hint="联网检索 Semantic Scholar / OpenAlex 做相似工作对比，新颖性核查更可靠；关闭则只查库内。"
          >
            <label className="row gap8" style={{ fontSize: 12.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={externalSearch} onChange={(e) => setExternalSearch(e.target.checked)} />
              开启外部检索
            </label>
          </FormField>
          <KnobRange
            label="评审修订轮数"
            en="revise_rounds"
            hint="自动评审与修订的最大轮数。"
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
            启动中…
          </>
        ) : (
          <>
            <Icon name="play" size={14} />
            开始深度生成
          </>
        )}
      </button>
      <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6, marginTop: 12 }}>
        全过程：目标构建（AI 带文献工具探索）→ 目标确认（人工）→ 方案起草（相关工作 / 设计 / 实验计划 /
        新颖性核查 / 风险）→ 多评审员评审与修订 → 入候选池。进度可在任务详情页实时查看。
      </div>
    </Drawer>
  );
}
