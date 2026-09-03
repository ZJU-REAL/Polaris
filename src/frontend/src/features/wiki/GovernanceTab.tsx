import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from '../../components/ui/Toast';
import { api, type DirectionLibraryDetail, type DuplicateCandidatePaper, type ProjectDefinition } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { InclusionSettingsForm, type InclusionValue } from '../libraries/InclusionSettingsForm';
import { InterdisciplinaryScopePanel } from '../projects/InterdisciplinaryScopePanel';

/** library.definition → 收录设置表单初值 */
function fromDefinition(def: ProjectDefinition | null): InclusionValue {
  const d = def ?? {};
  return {
    arxiv_categories: d.keywords?.arxiv_categories ?? [],
    include: d.keywords?.include ?? [],
    exclude: d.keywords?.exclude ?? [],
    rubric: d.rubric ?? [],
    anchors: d.anchor_papers ?? [],
  };
}

/* ============================================================
   文献库治理页签（P6）：
   - 库信息与预算编辑（可管理者）；
   - 本月 AI 用量进度（超限后同步任务暂停到下月）；
   - 重复论文候选与合并（不可撤销）。
   ============================================================ */

export function GovernanceTab({ libraryId, readOnly = false }: { libraryId: string; readOnly?: boolean }) {
  const { data: lib } = useQuery({
    queryKey: ['library', libraryId],
    queryFn: () => api.getLibrary(libraryId),
    retry: false,
  });

  return (
    <div className="col gap16" style={{ padding: 20, overflowY: 'auto' }}>
      {lib && <LibraryInfoCard lib={lib} readOnly={readOnly} />}
      {lib?.library_kind === 'interdisciplinary' && lib.project_id && (
        <InterdisciplinaryLibraryScope projectId={lib.project_id} library={lib} />
      )}
      {lib && (
        <InclusionSettingsCard
          lib={lib}
          readOnly={readOnly || lib.library_kind === 'interdisciplinary'}
        />
      )}
      {/* 预算 / 重复论文均为管理门数据，普通用户取不到 → 只读时不渲染 */}
      {!readOnly && (
        <>
          <BudgetCard libraryId={libraryId} />
          <DuplicatesCard libraryId={libraryId} />
        </>
      )}
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
  arxiv: { zh: '同一 arXiv 编号', en: 'Same arXiv id' },
  doi: { zh: '同一 DOI', en: 'Same DOI' },
  title: { zh: '标题相同', en: 'Same title' },
};

function DuplicatesCard({ libraryId }: { libraryId: string }) {
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
    onError: () => toast(tr('合并失败，请重试', 'Merge failed, please retry'), 'error'),
  });

  function confirmMerge(keep: DuplicateCandidatePaper, drop: DuplicateCandidatePaper) {
    const ok = window.confirm(
      `${tr('确定合并这两篇论文？', 'Merge these two papers?')}\n\n` +
        `${tr('保留：', 'Keep: ')}${keep.title}\n${tr('并入后删除：', 'Merge & delete: ')}${drop.title}\n\n` +
        tr(
          '被删除那篇的解读、笔记、划线、收藏等会全部并到保留的那篇上。此操作不可撤销。',
          'Its wiki, notes, highlights and stars will all move to the kept paper. This cannot be undone.',
        ),
    );
    if (ok) merge.mutate({ keep_id: keep.id, drop_id: drop.id });
  }

  return (
    <section className="card" style={{ padding: 18 }}>
      <h3 style={{ fontSize: 14, fontWeight: 700, marginBottom: 6 }}>
        {tr('重复论文', 'Duplicate papers')}
      </h3>
      <p className="muted" style={{ fontSize: 12, marginBottom: 12 }}>
        {tr(
          '合并会保留更完整的一篇，另一篇的内容并入后删除，不可撤销。',
          'Merging keeps the more complete row and folds the other into it, then deletes it — irreversible.',
        )}
      </p>
      {isLoading ? (
        <div className="skel" style={{ height: 48 }} />
      ) : isError ? (
        <div className="muted" style={{ fontSize: 13 }}>{tr('候选加载失败', 'Failed to load candidates')}</div>
      ) : !groups || groups.length === 0 ? (
        <div className="muted" style={{ fontSize: 13 }}>
          {tr('没有发现疑似重复的论文。', 'No suspected duplicates found.')}
        </div>
      ) : (
        <div className="col gap12">
          {groups.map((group, gi) => {
            const keep = group.papers[0];
            if (!keep) return null;
            return (
              <div
                key={`${group.reason}-${keep.id}-${gi}`}
                style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}
              >
                <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
                  {tr(REASON_LABEL[group.reason]?.zh ?? group.reason, REASON_LABEL[group.reason]?.en ?? group.reason)}
                </div>
                <div className="col gap8">
                  {group.papers.map((paper, pi) => (
                    <div key={paper.id} className="row" style={{ justifyContent: 'space-between', gap: 12 }}>
                      <div className="col" style={{ minWidth: 0 }}>
                        <div className="row gap8" style={{ minWidth: 0 }}>
                          <span style={{ fontSize: 13, fontWeight: pi === 0 ? 650 : 400, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {paper.title}
                          </span>
                          {pi === 0 && (
                            <span className="pill" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)', flexShrink: 0 }}>
                              {tr('建议保留', 'Suggested keep')}
                            </span>
                          )}
                        </div>
                        <span className="muted" style={{ fontSize: 12 }}>
                          {paper.year ?? tr('年份未知', 'Year unknown')} · {paper.source ?? tr('来源未知', 'Unknown source')} ·{' '}
                          {tr('全文分段 ', 'Chunks ')}{paper.chunk_count}
                          {paper.has_wiki ? ` · ${tr('已有解读', 'Has wiki')}` : ''}
                        </span>
                      </div>
                      {pi > 0 && (
                        <button
                          className="btn btn-soft sm"
                          disabled={merge.isPending}
                          onClick={() => confirmMerge(keep, paper)}
                          style={{ flexShrink: 0 }}
                        >
                          {tr('并入保留行', 'Merge into keep')}
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

/* —— 本月用量进度 —— */

function BudgetCard({ libraryId }: { libraryId: string }) {
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
    <section className="card" style={{ padding: 18 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
        <h3 style={{ fontSize: 14, fontWeight: 700 }}>{tr('本月 AI 用量', 'AI usage this month')}</h3>
        {budget && <span className="muted" style={{ fontSize: 12 }}>{budget.month}</span>}
      </div>
      {isError ? (
        <div className="muted" style={{ fontSize: 13 }}>{tr('用量加载失败', 'Failed to load usage')}</div>
      ) : !budget ? (
        <div className="skel" style={{ height: 34 }} />
      ) : (
        <div className="col gap8">
          <div className="row" style={{ justifyContent: 'space-between', fontSize: 13 }}>
            <span>
              {tr('已用 ', 'Used ')}
              <strong>{budget.used_tokens.toLocaleString()}</strong>
              {' tokens'}
              <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
                {tr('输入 ', 'in ')}{budget.prompt_tokens.toLocaleString()} · {tr('输出 ', 'out ')}
                {budget.completion_tokens.toLocaleString()}
              </span>
            </span>
            <span className="muted" style={{ fontSize: 12 }}>
              {limited
                ? `${tr('上限 ', 'Cap ')}${budget.monthly_budget!.toLocaleString()}`
                : tr('未设上限', 'No cap')}
            </span>
          </div>
          {limited && (
            <div style={{ height: 8, borderRadius: 4, background: 'var(--surface-3)', overflow: 'hidden' }}>
              <div
                style={{
                  width: `${Math.round(ratio * 100)}%`,
                  height: '100%',
                  borderRadius: 4,
                  background: barColor,
                  transition: 'width .3s',
                }}
              />
            </div>
          )}
          {budget.exhausted && (
            <div style={{ color: 'var(--danger-tx)', fontSize: 13 }}>
              {tr(
                '本月预算已用尽：同步任务已暂停，下月自动恢复，或调高上限后再试。',
                'Monthly budget used up: syncing is paused until next month, or raise the cap and retry.',
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

/* —— 库信息与预算 —— */

function LibraryInfoCard({
  lib,
  readOnly,
}: {
  lib: DirectionLibraryDetail;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(lib.name);
  const [statement, setStatement] = useState(lib.statement ?? '');
  const [budget, setBudget] = useState(lib.monthly_budget == null ? '' : String(lib.monthly_budget));

  // 库切换 / 保存后回填
  useEffect(() => {
    setName(lib.name);
    setStatement(lib.statement ?? '');
    setBudget(lib.monthly_budget == null ? '' : String(lib.monthly_budget));
  }, [lib]);

  const dirty =
    name !== lib.name ||
    statement !== (lib.statement ?? '') ||
    budget !== (lib.monthly_budget == null ? '' : String(lib.monthly_budget));

  const save = useMutation({
    mutationFn: () =>
      api.updateLibrary(lib.id, {
        name: name.trim() || lib.name,
        statement: statement.trim() || null,
        monthly_budget: budget.trim() === '' ? null : Math.max(0, Math.floor(Number(budget))),
      }),
    onSuccess: () => {
      toast(tr('库信息已保存', 'Library info saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library', lib.id] });
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
      void queryClient.invalidateQueries({ queryKey: ['library-budget', lib.id] });
    },
    onError: () => toast(tr('保存失败，请重试', 'Save failed, please retry'), 'error'),
  });

  const budgetInvalid = budget.trim() !== '' && (!Number.isFinite(Number(budget)) || Number(budget) < 0);

  return (
    <section className="card" style={{ padding: 18 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <h3 style={{ fontSize: 14, fontWeight: 700 }}>{tr('库信息', 'Library info')}</h3>
        {!readOnly && (
          <button
            className="btn btn-primary sm"
            disabled={!dirty || budgetInvalid || save.isPending || !name.trim()}
            onClick={() => save.mutate()}
          >
            {save.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
          </button>
        )}
      </div>
      <div className="col gap12">
        <label className="col gap6">
          <span className="muted" style={{ fontSize: 12 }}>{tr('名称', 'Name')}</span>
          <input className="input" value={name} disabled={readOnly} onChange={(e) => setName(e.target.value)} maxLength={255} />
        </label>
        <label className="col gap6">
          <span className="muted" style={{ fontSize: 12 }}>{tr('方向描述', 'Description')}</span>
          <textarea
            className="textarea"
            rows={3}
            style={{ resize: 'none', height: 78 }}
            value={statement}
            disabled={readOnly}
            onChange={(e) => setStatement(e.target.value)}
            placeholder={tr(
              '例：研究长时程运行的 LLM 智能体。关注记忆压缩、错误恢复、长期一致性评测；偏重方法与系统设计，不收纯 prompt 工程和纯应用报告。',
              'e.g. Long-running LLM agents. Focus on memory compaction, error recovery and long-horizon consistency evaluation; methods and system design rather than prompt engineering or application reports.',
            )}
          />
        </label>
        {!readOnly && (
        <div className="row gap12" style={{ flexWrap: 'wrap' }}>
          <label className="col gap6" style={{ minWidth: 220 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              {tr('每月 AI 预算（token 数，留空 = 不限）', 'Monthly AI budget (tokens, empty = unlimited)')}
            </span>
            <input
              className="input"
              inputMode="numeric"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              placeholder={tr('如 2000000', 'e.g. 2000000')}
            />
          </label>
        </div>
        )}
        {!readOnly && budgetInvalid && (
          <div style={{ color: 'var(--danger-tx)', fontSize: 12 }}>
            {tr('预算需为不小于 0 的数字', 'Budget must be a number ≥ 0')}
          </div>
        )}
      </div>
    </section>
  );
}

/* —— 收录设置（P8：库为收录配置权威源，ingest 按此检索/打分） —— */

function InclusionSettingsCard({ lib, readOnly }: { lib: DirectionLibraryDetail; readOnly?: boolean }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState<InclusionValue>(() => fromDefinition(lib.definition));

  useEffect(() => {
    setValue(fromDefinition(lib.definition));
  }, [lib]);

  const save = useMutation({
    mutationFn: () =>
      api.updateLibrary(lib.id, {
        keywords: {
          ...(lib.definition?.keywords ?? {}),
          arxiv_categories: value.arxiv_categories,
          include: value.include,
        },
        rubric: value.rubric.filter((r) => r.name.trim()),
        anchors: value.anchors.filter((a) => a.title.trim() || (a.arxiv_id ?? '').trim()),
      }),
    onSuccess: () => {
      toast(tr('收录设置已保存', 'Inclusion settings saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library', lib.id] });
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
    },
    onError: () => toast(tr('保存失败，请重试', 'Save failed, please retry'), 'error'),
  });

  return (
    <section className="card" style={{ padding: 18 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
        <h3 style={{ fontSize: 14, fontWeight: 700 }}>{tr('收录设置', 'Inclusion settings')}</h3>
        {!readOnly && (
          <button className="btn btn-primary sm" disabled={save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
          </button>
        )}
      </div>
      <p className="muted" style={{ fontSize: 12, marginBottom: 16 }}>
        {readOnly
          ? tr('由文献库管理员维护，你只能查看。', 'Maintained by library managers — read-only for you.')
          : tr(
              '文献追踪按这里的 arXiv 分类与关键词检索、按打分标准判定相关性；留空则用默认分类、只按方向说明打分。',
              'Literature tracking searches by these arXiv categories and keywords and scores relevance against the rubric; leave empty to use default categories and statement-only scoring.',
            )}
      </p>
      <InclusionSettingsForm
        value={value}
        onChange={setValue}
        showRubric
        readOnly={readOnly}
      />
    </section>
  );
}
