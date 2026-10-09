import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { fmtTime } from '../../lib/format';
import { api, ApiError, type DirectionLibrarySummary } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { StatementInterview } from './StatementInterview';
import { useLibraries, libraryPath } from './hooks';
import { DisciplineSelect } from './DisciplineSelect';
import {
  InclusionSettingsForm,
  hasInclusionKeywords,
  keywordsFromInclusion,
  type InclusionValue,
} from './InclusionSettingsForm';

/* ============================================================
   /libraries — 文献库列表（实验室区，P5c）
   卡片流：库名 / 方向陈述 / 论文·概念数 / 最近更新；点击进 /libraries/:id 详情。
   可在此新建独立文献库（与任何课题解耦）。
   ============================================================ */

/** 圆角小勾选框（体系内样式，替代原生 checkbox）。与论文撰写/实验搭建一致。 */
function CheckBox({ checked, onToggle, title }: { checked: boolean; onToggle: () => void; title?: string }) {
  return (
    <button
      type="button"
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
      style={{
        width: 18,
        height: 18,
        flexShrink: 0,
        borderRadius: 5,
        border: `1.5px solid ${checked ? 'var(--accent)' : 'var(--border-2)'}`,
        background: checked ? 'var(--accent)' : 'transparent',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        cursor: 'pointer',
        padding: 0,
        color: '#fff',
        transition: 'all .12s',
      }}
    >
      {checked && <Icon name="check" size={12} sw={2.4} />}
    </button>
  );
}

function LibraryCard({
  lib,
  admin,
  selectMode,
  selected,
  onOpen,
  onToggleSelect,
  onDelete,
}: {
  lib: DirectionLibrarySummary;
  admin: boolean;
  selectMode: boolean;
  selected: boolean;
  onOpen: () => void;
  onToggleSelect: () => void;
  onDelete: () => void;
}) {
  const updated = lib.last_compiled_at ?? lib.last_synced_at;
  const activate = selectMode ? onToggleSelect : onOpen;
  return (
    <div
      className="card hoverable"
      role="button"
      tabIndex={0}
      onClick={activate}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          activate();
        }
      }}
      style={{
        padding: '18px 20px',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        cursor: 'pointer',
        borderColor: selectMode && selected ? 'var(--accent)' : undefined,
      }}
    >
      <div className="row gap8" style={{ alignItems: 'flex-start' }}>
        {/* 占位常驻（仅 admin）：切换多选时卡片尺寸/位置不变 */}
        {admin && (
          <div
            style={{ paddingTop: 3, visibility: selectMode ? 'visible' : 'hidden' }}
            onClick={(e) => e.stopPropagation()}
          >
            <CheckBox
              checked={selected}
              onToggle={onToggleSelect}
              title={selected ? tr('取消选择', 'Deselect') : tr('选择', 'Select')}
            />
          </div>
        )}
        <span
          style={{
            width: 34,
            height: 34,
            borderRadius: 10,
            background: 'var(--accent-soft)',
            color: 'var(--accent)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <Icon name="book" size={17} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row gap8" style={{ flexWrap: 'wrap' }}>
            <span
              // 长库名限两行：撑高的是整行卡片（grid 行高取最高者），不只是它自己
              style={{
                fontSize: 14.5,
                fontWeight: 680,
                lineHeight: 1.3,
                display: '-webkit-box',
                WebkitLineClamp: 2,
                WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
                overflowWrap: 'anywhere',
              }}
              title={lib.name}
            >
              {lib.name}
            </span>
          </div>
        </div>
        <button
          className="icon-btn"
          title={tr('删除文献库', 'Delete library')}
          aria-label={tr('删除文献库', 'Delete library')}
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          style={{ flexShrink: 0, marginTop: -2, color: 'var(--text-4)' }}
        >
          <Icon name="trash" size={14} />
        </button>
      </div>
      <div
        style={{
          fontSize: 12.5,
          lineHeight: 1.55,
          color: 'var(--text-3)',
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
          minHeight: 38,
        }}
      >
        {lib.statement ?? tr('这个方向还没有写一句话介绍。', 'No statement for this direction yet.')}
      </div>
      <div
        className="row gap10"
        // marginTop:auto —— 同一行的卡片高度由最高的那张决定，不钉底的话矮内容的卡片
        // 会把统计行留在半空中（截图里 SWE 那张底下空一大块）
        style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 'auto' }}
      >
        <span className="row gap6">
          <Icon name="file" size={12} />
          {tr(`${lib.paper_count} 篇论文`, `${lib.paper_count} papers`)}
        </span>
        <span className="row gap6">
          <Icon name="layers" size={12} />
          {tr(`${lib.concept_count} 个概念`, `${lib.concept_count} concepts`)}
        </span>
        <span className="mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-4)' }}>
          {updated ? `${tr('更新于', 'Updated')} ${fmtTime(updated)}` : tr('还没有内容', 'Empty')}
        </span>
      </div>
    </div>
  );
}

const EMPTY_INCLUSION: InclusionValue = {
  sources: [],
  arxiv_categories: [],
  include: [],
  exclude: [],
  rubric: [],
  anchors: [],
};

/**
 * 新建文献库弹窗（P9b：任意登录用户可建）。名称 + 一句话说明必填；
 * 收录设置（分类 / 关键词 / 锚点论文 / 打分标准）共用 InclusionSettingsForm，
 * 可点「AI 自动生成」按名称+说明推荐一整套。提交后立即可用，跳详情页即可开始抓取。
 */
function NewLibraryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  // 学科决定论文按哪套字段抽取。建完库再去设置里改也行，但那之前抽过的论文
  // 已经按通用（机器学习形状的）口径存下来了——所以这个问题属于建库时
  const [discipline, setDiscipline] = useState('');
  const [interviewOpen, setInterviewOpen] = useState(false);
  const [incl, setIncl] = useState<InclusionValue>(EMPTY_INCLUSION);
  // 默认来源跟学科走（#821）：选「计算机科学」给 arXiv + cs.* 快捷项，选临床给 PubMed，
  // 不选学科给覆盖全学科的通用来源。用户动过来源勾选之后以他的为准（incl.sources 非空）
  const packsQuery = useQuery({ queryKey: ['disciplines'], queryFn: () => api.listDisciplines(), retry: false });
  const generalQuery = useQuery({
    queryKey: ['disciplines', 'defaults'],
    queryFn: () => api.getDisciplineDefaults(),
    retry: false,
  });
  const pack = (packsQuery.data ?? []).find((p) => p.name === discipline);
  // 包声明了来源就用它的；否则是部署默认（管理员在文献检索设置里配的）。两者都取不到时
  // 留空，不在前端编一个默认值——建库时服务端会按同样的规则补上
  const defaultSources = pack?.sources.length ? pack.sources : generalQuery.data?.sources ?? [];


  const mutation = useMutation({
    mutationFn: (input: Parameters<typeof api.createLibrary>[0]) => api.createLibrary(input),
    onSuccess: (lib) => {
      toast(tr('文献库已创建，可以开始抓取了', 'Library created — ingest can start now'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
      onClose();
      navigate(libraryPath(lib.id));
    },
    onError: (err) => {
      toast(`${tr('创建失败：', 'Create failed: ')}${err instanceof Error ? err.message : String(err)}`, 'error');
    },
  });

  function submit() {
    if (!name.trim()) {
      toast(tr('请填写文献库名称', 'Enter a library name'), 'info');
      return;
    }
    if (!statement.trim()) {
      toast(tr('请填写一句话说明', 'Enter a one-sentence statement'), 'info');
      return;
    }
    // 只选了来源、没填分类和关键词，也必须把 keywords 发出去：
    // 漏掉它等于用户挑的 PubMed 被悄悄丢掉，建出来的库还是抓 arXiv
    const keywords = hasInclusionKeywords(incl) ? keywordsFromInclusion(incl) : undefined;
    const anchors = incl.anchors
      .filter((a) => a.title.trim() || (a.arxiv_id ?? '').trim())
      .map((a) => ({
        title: a.title.trim(),
        ...(a.arxiv_id?.trim() ? { arxiv_id: a.arxiv_id.trim() } : {}),
        ...(a.reason?.trim() ? { reason: a.reason.trim() } : {}),
      }));
    const rubric = incl.rubric.filter((r) => r.name.trim());
    mutation.mutate({
      name: name.trim(),
      statement: statement.trim(),
      ...(discipline ? { discipline } : {}),
      ...(anchors.length > 0 ? { anchors } : {}),
      ...(keywords ? { keywords } : {}),
      ...(rubric.length > 0 ? { rubric } : {}),
    });
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr('新建文献库', 'New library')}
      sub={tr('创建后立即可用。', 'Ready to use right away.')}
      width={640}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary sm" disabled={mutation.isPending} onClick={submit}>
            {mutation.isPending ? tr('创建中…', 'Creating…') : tr('创建文献库', 'Create library')}
          </button>
        </>
      }
    >
      <div style={{ marginTop: 4 }}>
        <FormField label={tr('名称', 'Name')}>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)}
            placeholder={tr('如：稀疏注意力', 'e.g. Sparse attention')} />
        </FormField>
        <FormField
          label={tr('方向描述', 'Statement')}
          hint={tr('必填，用于挑论文和打分', 'Required — selects and scores papers')}
        >
          <div className="row gap8" style={{ alignItems: 'center', marginBottom: 6 }}>
            <button
              className="btn btn-soft sm"
              disabled={!name.trim()}
              title={
                name.trim()
                  ? tr('AI 按四个环节提问，帮你把方向说清楚', 'AI asks four questions to pin the direction down')
                  : tr('先填名称', 'Enter a name first')
              }
              onClick={() => setInterviewOpen(true)}
            >
              <Icon name="sparkle" size={12} />
              {tr('AI 访谈生成', 'Write it with AI')}
            </button>
            <span className="muted" style={{ fontSize: 11.5 }}>
              {tr('不知道怎么写就用它', 'Use this if you are unsure what to write')}
            </span>
          </div>
          <textarea className="textarea" rows={2} value={statement} onChange={(e) => setStatement(e.target.value)}
            placeholder={tr(
              '例：研究长时程运行的 LLM 智能体。关注记忆压缩、错误恢复、长期一致性评测；偏重方法与系统设计，不收纯 prompt 工程和纯应用报告。',
              'e.g. Long-running LLM agents. Focus on memory compaction, error recovery and long-horizon consistency evaluation; methods and system design rather than prompt engineering or application reports.',
            )} />
          <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5, marginTop: 4 }}>
            {tr(
              '写清四件事效果最好：研究问题、研究对象、关注的子问题、偏重哪类方法。用英文写——语料是英文论文摘要，中文描述会让向量匹配失准。',
              'Four things help most: the research question, the subject, the sub-problems, and which kinds of method you favour. Write it in English — the corpus is English abstracts, and a Chinese statement skews vector matching.',
            )}
          </div>
        </FormField>
        <div className="hr" style={{ margin: '4px 0 16px' }} />
        <FormField label={tr('学科口径', 'Discipline')}>
          <DisciplineSelect value={discipline} onChange={setDiscipline} />
          <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5, marginTop: 4 }}>
            {tr(
              '决定论文的方法卡按哪套字段抽取。通用口径是机器学习的形状（目的、手段、基线、数据集），选一个学科后换成该领域自己的字段。',
              'Sets which fields the method card is extracted into. The general fields are shaped for machine learning (purpose, mechanism, baseline, dataset); picking a discipline swaps in that field’s own.',
            )}
          </div>
        </FormField>
        <div className="hr" style={{ margin: '4px 0 16px' }} />
        <InclusionSettingsForm
          value={incl}
          onChange={setIncl}
          showRubric
          defaultSources={defaultSources}
          arxivQuickPicks={pack?.arxiv_categories ?? []}
        />
      </div>
      <StatementInterview
        open={interviewOpen}
        topic={name}
        onClose={() => setInterviewOpen(false)}
        onDone={setStatement}
      />
    </Modal>
  );
}

export function LibrariesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useLibraries();
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const canCreate = !!me;
  const admin = !!me;
  const [createOpen, setCreateOpen] = useState(false);
  // 多选态（仅 admin 可用）：selectMode 打开后每张卡可勾选，顶部出现批量操作栏
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const libraries = data ?? [];
  const sorted = [...libraries].sort((a, b) => a.name.localeCompare(b.name));

  function toggleSelect(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function exitSelect() {
    setSelectMode(false);
    setSelectedIds(new Set());
  }
  function clearSelection() {
    setSelectedIds(new Set());
  }
  const allSelected = sorted.length > 0 && sorted.every((l) => selectedIds.has(l.id));
  function toggleSelectAll() {
    setSelectedIds(allSelected ? new Set() : new Set(sorted.map((l) => l.id)));
  }

  // 删除单个库：409 LIBRARY_HAS_TOPICS 时二次确认后带 force 重删。
  // 返回 true = 已删除，false = 用户在二次确认里放弃。
  async function deleteOne(lib: DirectionLibrarySummary): Promise<boolean> {
    try {
      await api.deleteLibrary(lib.id);
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.message.includes('LIBRARY_HAS_TOPICS')) {
        const ok = window.confirm(
          tr(
            `文献库${lib.name}仍被课题关联，确定连同关联一起删除吗？`,
            `Library “${lib.name}” is still linked to topics — delete it together with those links?`,
          ),
        );
        if (!ok) return false;
        await api.deleteLibrary(lib.id, true);
        return true;
      }
      throw e;
    }
  }

  // 单删 / 批量删都走这个 mutation（批量按选中项串行处理，逐个弹 409 确认）。
  const deleteMutation = useMutation({
    mutationFn: async (targets: DirectionLibrarySummary[]) => {
      let deleted = 0;
      let skipped = 0;
      const failed: string[] = [];
      for (const lib of targets) {
        try {
          if (await deleteOne(lib)) deleted += 1;
          else skipped += 1;
        } catch (e) {
          failed.push(e instanceof Error ? e.message : String(e));
        }
      }
      return { deleted, skipped, failed };
    },
    onSuccess: ({ deleted, skipped, failed }) => {
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
      clearSelection();
      if (failed.length > 0) {
        toast(
          tr(`已删除 ${deleted} 个，${failed.length} 个失败：${failed[0]}`, `Deleted ${deleted}, ${failed.length} failed: ${failed[0]}`),
          'error',
        );
      } else if (deleted > 0) {
        toast(
          tr(`已删除 ${deleted} 个文献库${skipped ? `，跳过 ${skipped} 个` : ''}`, `Deleted ${deleted} librar${deleted === 1 ? 'y' : 'ies'}${skipped ? `, skipped ${skipped}` : ''}`),
          'ok',
        );
      } else {
        toast(tr('已取消删除', 'Deletion cancelled'), 'info');
      }
    },
    onError: (err) => toast(`${tr('删除失败：', 'Delete failed: ')}${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  return (
    <div className="page fadeup" style={{ maxWidth: 1200 }}>
      {/* —— 筛选行：操作并在同一行右侧 —— */}
      <div className="row gap12" style={{ margin: '0 0 16px', flexWrap: 'wrap', alignItems: 'center' }}>
        {canCreate && (
          <button
            className="btn btn-primary sm"
            style={{ marginLeft: 'auto' }}
            onClick={() => setCreateOpen(true)}
          >
            <Icon name="plus" size={13} />
            {tr('新建文献库', 'New library')}
          </button>
        )}
        {admin && sorted.length > 0 && (
          <button
            className={`btn sm ${selectMode ? 'btn-primary' : 'btn-soft'}`}
            onClick={() => (selectMode ? exitSelect() : setSelectMode(true))}
            title={tr('批量选择', 'Multi-select')}
          >
            <Icon name="check" size={13} />
            {tr('多选', 'Multi-select')}
          </button>
        )}
        {/* 全选放工具栏：不再插入额外行导致卡片下移 */}
        {admin && selectMode && sorted.length > 0 && (
          <div className="row gap8" style={{ alignItems: 'center' }}>
            <CheckBox checked={allSelected} onToggle={toggleSelectAll} title={tr('全选', 'Select all')} />
            <span className="muted" style={{ fontSize: 12 }}>
              {selectedIds.size > 0
                ? tr(`已选 ${selectedIds.size} 个`, `${selectedIds.size} selected`)
                : tr('全选', 'Select all')}
            </span>
          </div>
        )}
      </div>

      {canCreate && <NewLibraryModal open={createOpen} onClose={() => setCreateOpen(false)} />}

      {isLoading ? (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(min(320px, 100%), 1fr))',
            gap: 14,
          }}
        >
          {[0, 1, 2].map((i) => (
            <div key={i} className="skel" style={{ height: 150, borderRadius: 14 }} />
          ))}
        </div>
      ) : isError ? (
        <EmptyState
          icon="x"
          title={tr('无法加载文献库列表', 'Failed to load libraries')}
          desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or API not ready.')}
          action={
            <button className="btn btn-soft sm" onClick={() => void refetch()}>
              {tr('重试', 'Retry')}
            </button>
          }
        />
      ) : sorted.length === 0 ? (
        <EmptyState
          icon="book"
          title={tr('还没有文献库', 'No libraries yet')}
          desc={
            canCreate
              ? undefined
              : tr('创建课题后会自动生成对应方向的文献库。', 'A direction library is created together with each topic.')
          }
          action={
            canCreate ? (
              <button className="btn btn-primary sm" onClick={() => setCreateOpen(true)}>
                <Icon name="plus" size={13} />
                {tr('新建文献库', 'New library')}
              </button>
            ) : (
              <button className="btn btn-primary sm" onClick={() => navigate('/projects/new')}>
                <Icon name="plus" size={13} />
                {tr('新建课题', 'New topic')}
              </button>
            )
          }
        />
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(min(320px, 100%), 1fr))',
            gap: 14,
          }}
        >
          {sorted.map((lib) => (
            <LibraryCard
              key={lib.id}
              lib={lib}
              admin={admin}
              selectMode={selectMode}
              selected={selectedIds.has(lib.id)}
              onOpen={() => navigate(libraryPath(lib.id))}
              onToggleSelect={() => toggleSelect(lib.id)}
              onDelete={() => {
                const ok = window.confirm(
                  tr(
                    `确定删除文献库${lib.name}吗？此操作不可撤销。`,
                    `Delete library "${lib.name}"? This cannot be undone.`,
                  ),
                );
                if (!ok) return;
                deleteMutation.mutate([lib]);
              }}
            />
          ))}
        </div>
      )}

      {/* 批量操作栏（多选模式下有选中时浮出底部，与论文撰写/实验搭建一致） */}
      {admin && selectMode && selectedIds.size > 0 && (
        <div
          className="card card-pad"
          style={{
            position: 'sticky',
            bottom: 16,
            marginTop: 16,
            boxShadow: 'var(--shadow-pop)',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
          }}
        >
          <span style={{ fontSize: 12.5, fontWeight: 600 }}>
            {tr(`已选 ${selectedIds.size} 个`, `${selectedIds.size} selected`)}
          </span>
          <div className="row gap8" style={{ marginLeft: 'auto' }}>
            <button
              className="btn btn-danger sm"
              disabled={deleteMutation.isPending}
              onClick={() => {
                const targets = sorted.filter((l) => selectedIds.has(l.id));
                if (targets.length === 0) return;
                const ok = window.confirm(
                  tr(
                    `确定删除选中的 ${targets.length} 个文献库吗？此操作不可撤销。`,
                    `Delete ${targets.length} selected libraries? This cannot be undone.`,
                  ),
                );
                if (!ok) return;
                deleteMutation.mutate(targets);
              }}
            >
              <Icon name="trash" size={13} />
              {deleteMutation.isPending ? tr('删除中…', 'Deleting…') : tr('批量删除', 'Delete selected')}
            </button>
            <button className="btn btn-ghost sm" disabled={deleteMutation.isPending} onClick={clearSelection}>
              {tr('取消选择', 'Clear')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
