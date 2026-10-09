import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from '../../components/ui/Toast';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { api, type DirectionLibraryDetail, type DuplicateCandidatePaper, type ProjectDefinition } from '../../lib/api';
import { tr } from '../../lib/i18n';
import {
  InclusionSettingsForm,
  keywordsFromInclusion,
  type InclusionValue,
} from '../libraries/InclusionSettingsForm';
import { InterdisciplinaryScopePanel } from '../projects/InterdisciplinaryScopePanel';
import { DisciplineSelect, disciplineFieldValue } from '../libraries/DisciplineSelect';
import { PanelSection as Section, PanelHint as SectionHint } from './shared';

/** library.definition → 收录设置表单初值 */
function fromDefinition(def: ProjectDefinition | null): InclusionValue {
  const d = def ?? {};
  return {
    sources: d.keywords?.sources ?? [],
    arxiv_categories: d.keywords?.arxiv_categories ?? [],
    include: d.keywords?.include ?? [],
    exclude: d.keywords?.exclude ?? [],
    rubric: d.rubric ?? [],
    anchors: d.anchor_papers ?? [],
  };
}

/* ============================================================
   文献库设置页签（原「治理」）：
   - 基本信息 / 学科 / 收录设置：各自保存，只有改动过才出现保存按钮，
     同一时刻页面上通常只有一个主按钮；
   - 本月用量展示（#734 起纯展示：预算硬限额已移除，不再暂停任务，
     旧的「每月预算」输入随之撤下——参考上限仅在用量条上呈现）；
   - 重复论文候选与合并（不可撤销）。
   版式：单列、分节标签在内容外、行内左标签右控件，不再一节一张卡
   （外层工作台本身就是一张卡，再套卡就是卡中卡）。
   ============================================================ */

function SaveButton({ pending, disabled, onClick }: { pending: boolean; disabled?: boolean; onClick: () => void }) {
  return (
    <button className="btn btn-primary sm" disabled={pending || disabled} onClick={onClick}>
      {pending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
    </button>
  );
}

export function GovernanceTab({ libraryId }: { libraryId: string }) {
  const { data: lib } = useQuery({
    queryKey: ['library', libraryId],
    queryFn: () => api.getLibrary(libraryId),
    retry: false,
  });

  return (
    <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
      <div className="col" style={{ gap: 20, maxWidth: 760, margin: '0 auto', padding: '4px 24px 48px' }}>
        {lib && <LibraryInfoSection lib={lib} />}
        {lib?.library_kind === 'interdisciplinary' && lib.project_id && (
          <InterdisciplinaryLibraryScope projectId={lib.project_id} library={lib} />
        )}
        {lib && <DisciplineSection lib={lib} />}
        {lib && (
          // 交叉库的收录范围由课题的交叉研究设置决定，这里只读
          <InclusionSettingsSection lib={lib} readOnly={lib.library_kind === 'interdisciplinary'} />
        )}
        <BudgetSection libraryId={libraryId} />
        <DuplicatesSection libraryId={libraryId} />
      </div>
    </div>
  );
}

function InterdisciplinaryLibraryScope({
  projectId,
  library,
}: {
  projectId: string;
  library: DirectionLibraryDetail;
}) {
  const projectQuery = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.getProject(projectId),
    retry: false,
  });

  if (projectQuery.isLoading) {
    return <section className="card interdisciplinary-profile-card"><div className="skel interdisciplinary-profile-skeleton" /></section>;
  }
  if (!projectQuery.data) return null;
  return <InterdisciplinaryScopePanel project={projectQuery.data} dedicatedLibrary={library} />;
}

/* —— 重复论文候选与合并 —— */

const REASON_LABEL: Record<string, { zh: string; en: string }> = {
  arxiv: { zh: '同一 arXiv 编号', en: 'Same arXiv ID' },
  doi: { zh: '同一 DOI', en: 'Same DOI' },
  title: { zh: '标题相同', en: 'Same title' },
};

function DuplicatesSection({ libraryId }: { libraryId: string }) {
  const queryClient = useQueryClient();
  const { data: groups, isLoading, isError } = useQuery({
    queryKey: ['library-duplicates', libraryId],
    queryFn: () => api.listDuplicateCandidates(libraryId),
    retry: false,
  });

  const merge = useMutation({
    mutationFn: (input: { keep_id: string; drop_id: string }) => api.mergePapers(input),
    onSuccess: () => {
      toast(tr('已合并为一篇论文', 'Merged into one paper'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library-duplicates', libraryId] });
      void queryClient.invalidateQueries({ queryKey: ['library', libraryId] });
      void queryClient.invalidateQueries({ queryKey: ['papers'] });
    },
    onError: () => toast(tr('无法合并，请重试', 'Couldn’t merge. Try again.'), 'error'),
  });

  const [pendingMerge, setPendingMerge] = useState<{ keep: DuplicateCandidatePaper; drop: DuplicateCandidatePaper } | null>(null);

  return (
    <Section label={tr('重复论文', 'Duplicate papers')}>
      <SectionHint>
        {tr(
          '合并后只保留一篇，另一篇的笔记和解读会并入其中。',
          'Merging keeps one paper and moves the other’s notes and summary into it.',
        )}
      </SectionHint>
      {isLoading ? (
        <div className="skel" style={{ height: 40 }} />
      ) : isError ? (
        <div className="muted" style={{ fontSize: 13 }}>{tr('无法加载重复论文', 'Couldn’t load duplicates')}</div>
      ) : !groups || groups.length === 0 ? (
        <div className="muted" style={{ fontSize: 13 }}>{tr('没有疑似重复的论文', 'No likely duplicates')}</div>
      ) : (
        <div className="settings-list">
          {groups.map((group, gi) => {
            const keep = group.papers[0];
            if (!keep) return null;
            return (
              <div key={`${group.reason}-${keep.id}-${gi}`} className="col" style={{ gap: 8, padding: '12px 0' }}>
                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
                  {tr(REASON_LABEL[group.reason]?.zh ?? group.reason, REASON_LABEL[group.reason]?.en ?? group.reason)}
                </div>
                {group.papers.map((paper, pi) => (
                  <div key={paper.id} className="row" style={{ justifyContent: 'space-between', gap: 12 }}>
                    <div className="col" style={{ minWidth: 0 }}>
                      <div className="row gap8" style={{ minWidth: 0 }}>
                        <span
                          className="ellipsis"
                          title={paper.title}
                          style={{ fontSize: 13, fontWeight: pi === 0 ? 600 : 400, minWidth: 0 }}
                        >
                          {paper.title}
                        </span>
                        {pi === 0 && (
                          <span style={{ fontSize: 12, color: 'var(--accent-text)', flexShrink: 0 }}>
                            {tr('建议保留', 'Keep (suggested)')}
                          </span>
                        )}
                      </div>
                      <span className="muted" style={{ fontSize: 12 }}>
                        {paper.year ?? tr('年份未知', 'Year unknown')} · {paper.source ?? tr('来源未知', 'Unknown source')} ·{' '}
                        {paper.chunk_count > 0 ? tr('有全文', 'Full text') : tr('无全文', 'No full text')}
                        {paper.has_wiki ? ` · ${tr('有解读', 'Summarized')}` : ''}
                      </span>
                    </div>
                    {pi > 0 && (
                      <button
                        className="btn btn-ghost sm"
                        disabled={merge.isPending}
                        onClick={() => setPendingMerge({ keep, drop: paper })}
                        style={{ flexShrink: 0 }}
                      >
                        {tr('合并', 'Merge')}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      )}
      <ConfirmModal
        open={!!pendingMerge}
        onClose={() => setPendingMerge(null)}
        title={tr('合并这两篇论文？', 'Merge these papers?')}
        message={
          pendingMerge
            ? tr(
                `「${pendingMerge.drop.title}」的笔记、划线和解读会并入「${pendingMerge.keep.title}」，然后被删除。此操作无法撤销。`,
                `Notes, highlights and the summary of “${pendingMerge.drop.title}” move into “${pendingMerge.keep.title}”, then it’s deleted. This can’t be undone.`,
              )
            : ''
        }
        confirmText={tr('合并', 'Merge')}
        danger
        busy={merge.isPending}
        onConfirm={() => {
          if (pendingMerge) merge.mutate({ keep_id: pendingMerge.keep.id, drop_id: pendingMerge.drop.id });
          setPendingMerge(null);
        }}
      />
    </Section>
  );
}

/* —— 本月用量展示（原「预算进度」；上限只是参考，不再是闸门） —— */

function BudgetSection({ libraryId }: { libraryId: string }) {
  const { data: budget, isError } = useQuery({
    queryKey: ['library-budget', libraryId],
    queryFn: () => api.getLibraryBudget(libraryId),
    retry: false,
    refetchInterval: 60_000,
  });

  const limited = budget?.monthly_budget != null && budget.monthly_budget > 0;
  const ratio = limited && budget ? Math.min(1, budget.used_tokens / budget.monthly_budget!) : 0;
  const barColor = ratio >= 1 ? 'var(--danger)' : ratio >= 0.8 ? 'var(--warn)' : 'var(--accent)';

  return (
    <Section
      label={tr('本月用量', 'Usage this month')}
      action={budget ? <span className="muted mono" style={{ fontSize: 12 }}>{budget.month}</span> : undefined}
    >
      {isError ? (
        <div className="muted" style={{ fontSize: 13 }}>{tr('无法加载用量', 'Couldn’t load usage')}</div>
      ) : !budget ? (
        <div className="skel" style={{ height: 34 }} />
      ) : (
        <div className="col gap8">
          <div className="row" style={{ justifyContent: 'space-between', fontSize: 13, gap: 12, flexWrap: 'wrap' }}>
            <span>
              {tr('已用 ', 'Used ')}
              <strong style={{ fontVariantNumeric: 'tabular-nums' }}>{budget.used_tokens.toLocaleString()}</strong>
              {' tokens'}
              <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
                {tr('输入 ', 'Input ')}{budget.prompt_tokens.toLocaleString()} · {tr('输出 ', 'Output ')}
                {budget.completion_tokens.toLocaleString()}
              </span>
            </span>
            <span className="muted" style={{ fontSize: 12 }}>
              {limited
                ? `${tr('参考上限 ', 'Reference limit ')}${budget.monthly_budget!.toLocaleString()}`
                : tr('未设参考上限', 'No reference limit')}
            </span>
          </div>
          {limited && (
            <div style={{ height: 6, borderRadius: 3, background: 'var(--surface-3)', overflow: 'hidden' }}>
              <div
                style={{
                  width: `${Math.round(ratio * 100)}%`,
                  height: '100%',
                  borderRadius: 3,
                  background: barColor,
                  transition: 'width .3s',
                }}
              />
            </div>
          )}
          {budget.exhausted && (
            <SectionHint>
              {tr(
                '本月用量已超过参考上限，任务照常运行。',
                'Usage is over this month’s reference limit. Tasks keep running.',
              )}
            </SectionHint>
          )}
        </div>
      )}
    </Section>
  );
}

/* —— 基本信息 —— */

function LibraryInfoSection({ lib }: { lib: DirectionLibraryDetail }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(lib.name);
  const [statement, setStatement] = useState(lib.statement ?? '');

  // 库切换 / 保存后回填
  useEffect(() => {
    setName(lib.name);
    setStatement(lib.statement ?? '');
  }, [lib]);

  const dirty = name !== lib.name || statement !== (lib.statement ?? '');

  // 「每月预算」输入已撤（#734）：硬限额移除后它什么都不控制，留着只会误导。
  const save = useMutation({
    mutationFn: () =>
      api.updateLibrary(lib.id, {
        name: name.trim() || lib.name,
        statement: statement.trim() || null,
      }),
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library', lib.id] });
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
      void queryClient.invalidateQueries({ queryKey: ['library-budget', lib.id] });
    },
    onError: () => toast(tr('无法保存，请重试', 'Couldn’t save. Try again.'), 'error'),
  });

  return (
    <Section
      first
      label={tr('基本信息', 'Details')}
      action={dirty ? <SaveButton pending={save.isPending} disabled={!name.trim()} onClick={() => save.mutate()} /> : undefined}
    >
      <div className="settings-list">
        <div className="settings-row" style={{ flexWrap: 'wrap' }}>
          <div className="settings-row-text" style={{ fontSize: 13 }}>{tr('名称', 'Name')}</div>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={255}
            style={{ width: 360, maxWidth: '100%' }}
          />
        </div>
        <div className="col" style={{ gap: 8, padding: '12px 0 4px' }}>
          <div style={{ fontSize: 13 }}>{tr('方向说明', 'Scope')}</div>
          <textarea
            className="textarea"
            rows={3}
            style={{ resize: 'vertical', minHeight: 72 }}
            value={statement}
            onChange={(e) => setStatement(e.target.value)}
            placeholder={tr(
              '例如 Long-running LLM agents: memory compaction, error recovery, long-horizon evaluation. No pure prompt engineering.',
              'e.g. Long-running LLM agents: memory compaction, error recovery, long-horizon evaluation. No pure prompt engineering.',
            )}
          />
          <SectionHint>{tr('用来判断论文是否相关，建议用英文写。', 'Used to judge relevance. English works best.')}</SectionHint>
        </div>
      </div>
    </Section>
  );
}

/* —— 收录设置（P8：库为收录配置权威源，ingest 按此检索/打分） —— */

/**
 * 来源为空的库当年的含义：只用 arXiv。新库建库时就按学科写入了来源（#821），走到这个
 * 回退的只有存量库——与后端 actions_wiki.DEFAULT_LIBRARY_SOURCES 同口径。
 */
const LEGACY_LIBRARY_SOURCES = ['arxiv'];

function InclusionSettingsSection({ lib, readOnly }: { lib: DirectionLibraryDetail; readOnly?: boolean }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState<InclusionValue>(() => fromDefinition(lib.definition));
  // arXiv 分类快捷项跟库的学科走；没选学科就只有自由输入
  const packsQuery = useQuery({ queryKey: ['disciplines'], queryFn: () => api.listDisciplines(), retry: false });
  const arxivQuickPicks = (packsQuery.data ?? []).find((p) => p.name === lib.discipline)?.arxiv_categories ?? [];

  useEffect(() => {
    setValue(fromDefinition(lib.definition));
  }, [lib]);

  // 只有改动过才出现保存按钮：页面同时只露一个主按钮
  const dirty = JSON.stringify(value) !== JSON.stringify(fromDefinition(lib.definition));

  const save = useMutation({
    mutationFn: () =>
      api.updateLibrary(lib.id, {
        keywords: { ...(lib.definition?.keywords ?? {}), ...keywordsFromInclusion(value) },
        rubric: value.rubric.filter((r) => r.name.trim()),
        anchors: value.anchors.filter((a) => a.title.trim() || (a.arxiv_id ?? '').trim()),
      }),
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library', lib.id] });
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
    },
    onError: () => toast(tr('无法保存，请重试', 'Couldn’t save. Try again.'), 'error'),
  });

  return (
    <Section
      label={tr('收录设置', 'Inclusion')}
      action={!readOnly && dirty ? <SaveButton pending={save.isPending} onClick={() => save.mutate()} /> : undefined}
    >
      <SectionHint>
        {readOnly
          ? tr(
              '交叉库的收录范围跟随课题设置，这里只能查看。',
              'This cross-field library follows its topic’s settings, so it’s read-only here.',
            )
          : tr(
              '决定从哪里检索、用哪些关键词，以及如何判断相关。',
              'Choose where to search, which keywords to use and how relevance is judged.',
            )}
      </SectionHint>
      <InclusionSettingsForm
        value={value}
        onChange={setValue}
        showRubric
        readOnly={readOnly}
        // 新库建库时就写入了来源；来源为空的只有存量库，而它们当年的含义是只用 arXiv
        defaultSources={LEGACY_LIBRARY_SOURCES}
        arxivQuickPicks={arxivQuickPicks}
      />
    </Section>
  );
}

/* —— 学科口径（#775）——
   装了学科包却没有入口的话，包里那套字段永远用不上：抽取照旧按机器学习的口径
   （baseline / dataset）走，而做结构、做合成、做临床的人看不出为什么方法卡答非所问。
   这一节就是那个入口。 */

/**
 * 选择框的值 → PATCH 里的 discipline。
 *
 * 保留这个再导出：既有用例按这个名字断言「清空要发 null 而不是空串」，
 * 而那条判据本身搬到了共用组件里。
 */
export const disciplinePatchValue = disciplineFieldValue;

function DisciplineSection({ lib }: { lib: DirectionLibraryDetail }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState<string>(lib.discipline ?? '');

  useEffect(() => setValue(lib.discipline ?? ''), [lib.id, lib.discipline]);

  const save = useMutation({
    // 空字符串 = 清空，回到内置口径；后端按 null 处理
    mutationFn: () => api.updateLibrary(lib.id, { discipline: disciplinePatchValue(value) }),
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library', lib.id] });
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
    },
    onError: () => toast(tr('无法保存，请重试', 'Couldn’t save. Try again.'), 'error'),
  });

  const dirty = (lib.discipline ?? '') !== value;

  return (
    <Section
      label={tr('学科', 'Discipline')}
      action={dirty ? <SaveButton pending={save.isPending} onClick={() => save.mutate()} /> : undefined}
    >
      <SectionHint>
        {tr(
          '决定方法卡提取哪些字段，只影响之后处理的论文。',
          'Sets the fields extracted into method cards. Applies to papers processed from now on.',
        )}
      </SectionHint>
      <DisciplineSelect value={value} onChange={setValue} maxWidth={360} compact />
    </Section>
  );
}
