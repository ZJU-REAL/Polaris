import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { EmptyState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { fmtTime } from '../../lib/format';
import { api, ApiError, isAdmin, type DirectionLibrarySummary } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useLibraries, libraryPath } from './hooks';
import { InclusionSettingsForm, ARXIV_ID_RE, type InclusionValue } from './InclusionSettingsForm';

/* ============================================================
   /libraries — 文献库列表（实验室区，P5c）
   卡片流：库名 / 方向陈述 / 论文·概念数 / 最近更新；
   「我的课题关联的库」有标识；点击进 /libraries/:id 详情。
   平台管理员可在此新建独立共享文献库（与任何课题解耦）。
   ============================================================ */

const CADENCES = [
  { v: 'daily', zh: '每日', en: 'Daily' },
  { v: 'weekly', zh: '每周', en: 'Weekly' },
  { v: 'manual', zh: '手动', en: 'Manual' },
] as const;

function StatusBadge({ status }: { status: DirectionLibrarySummary['status'] }) {
  if (status === 'active') return null;
  const cfg =
    status === 'pending'
      ? { zh: '待审批', en: 'Pending', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' }
      : { zh: '已驳回', en: 'Rejected', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' };
  return (
    <span className="pill sm" style={{ background: cfg.bg, color: cfg.tx, flexShrink: 0 }}>
      {tr(cfg.zh, cfg.en)}
    </span>
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
        outline: selectMode && selected ? '2px solid var(--accent)' : undefined,
        outlineOffset: -2,
      }}
    >
      <div className="row gap8" style={{ alignItems: 'flex-start' }}>
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
          <div className="row gap8">
            <span style={{ fontSize: 14.5, fontWeight: 680, lineHeight: 1.3 }} title={lib.name}>
              {lib.name}
            </span>
            {lib.is_mine && (
              <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)', flexShrink: 0 }}>
                {tr('我在用', 'In use')}
              </span>
            )}
            <StatusBadge status={lib.status} />
          </div>
        </div>
        {selectMode ? (
          <input
            type="checkbox"
            checked={selected}
            readOnly
            aria-label={tr('选择', 'Select')}
            style={{ width: 16, height: 16, accentColor: 'var(--accent)', flexShrink: 0, marginTop: 3, cursor: 'pointer' }}
          />
        ) : admin ? (
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
        ) : (
          <Icon name="arrow" size={14} style={{ color: 'var(--text-4)', flexShrink: 0, marginTop: 4 }} />
        )}
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
      <div className="row gap10" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
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

const EMPTY_INCLUSION: InclusionValue = { arxiv_categories: [], include: [], rubric: [], anchors: [] };

/**
 * 新建文献库弹窗（P9b：任意登录用户可建）。名称 + 一句话说明必填；
 * 收录设置（分类 / 关键词 / 锚点论文 / 打分标准）共用 InclusionSettingsForm，
 * 可点「AI 自动生成」按名称+说明推荐一整套。提交后建 pending 库，跳详情页，
 * 等管理员审批激活后才能开始抓取。
 */
function NewLibraryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  const [cadence, setCadence] = useState<string>('daily');
  const [incl, setIncl] = useState<InclusionValue>(EMPTY_INCLUSION);

  const badAnchors = incl.anchors.filter(
    (a) => !!a.arxiv_id && a.arxiv_id.trim() !== '' && !ARXIV_ID_RE.test(a.arxiv_id.trim()),
  );

  const mutation = useMutation({
    mutationFn: (input: Parameters<typeof api.createLibrary>[0]) => api.createLibrary(input),
    onSuccess: (lib) => {
      toast(
        tr('已提交，待管理员审批激活后即可开始抓取', 'Submitted — an admin will review and activate it before ingest can start'),
        'ok',
      );
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
    if (badAnchors.length > 0) {
      toast(tr('有锚点论文填了非法 arXiv 编号，请修正后再提交', 'Some anchor papers have invalid arXiv ids — fix them first'), 'error');
      return;
    }
    const keywords =
      incl.arxiv_categories.length > 0 || incl.include.length > 0
        ? { arxiv_categories: incl.arxiv_categories, include: incl.include }
        : undefined;
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
      ...(anchors.length > 0 ? { anchors } : {}),
      ...(keywords ? { keywords } : {}),
      ...(rubric.length > 0 ? { rubric } : {}),
      cadence,
    });
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr('新建文献库', 'New library')}
      sub={tr(
        '先填好方向，提交后由管理员审批激活；激活后才会开始抓取，创建本身不花额度。',
        'Describe the direction and submit; an admin activates it. Ingest starts only after activation — creating costs nothing.',
      )}
      width={640}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary sm" disabled={mutation.isPending} onClick={submit}>
            {mutation.isPending ? tr('提交中…', 'Submitting…') : tr('提交待审批', 'Submit for review')}
          </button>
        </>
      }
    >
      <div style={{ marginTop: 4 }}>
        <FormField label={tr('名称', 'Name')} hint={tr('将显示在文献库列表中', 'Shown in the library list')}>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)}
            placeholder={tr('如：稀疏注意力', 'e.g. Sparse attention')} />
        </FormField>
        <FormField label={tr('一句话说明', 'Statement')} hint={tr('这个方向研究什么（必填，用于相关性打分）', 'What this direction studies (required, used for relevance scoring)')}>
          <textarea className="textarea" rows={2} value={statement} onChange={(e) => setStatement(e.target.value)}
            placeholder={tr('用一句话介绍这个文献库的方向', 'One sentence describing this library’s direction')} />
        </FormField>
        <FormField label={tr('运行节奏', 'Cadence')} hint={tr('激活后自动同步的运行频率', 'How often ingest runs after activation')}>
          <div>
            <Segmented options={CADENCES.map((c) => ({ v: c.v, label: tr(c.zh, c.en) }))}
              value={cadence as (typeof CADENCES)[number]['v']} onChange={(v) => setCadence(v)} />
          </div>
        </FormField>
        <div className="hr" style={{ margin: '4px 0 16px' }} />
        <InclusionSettingsForm
          value={incl}
          onChange={setIncl}
          name={name}
          statement={statement}
          showRubric
          showAnchors
        />
      </div>
    </Modal>
  );
}

export function LibrariesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useLibraries();
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const canCreate = !!me;
  const admin = isAdmin(me);
  const [createOpen, setCreateOpen] = useState(false);
  // 多选态（仅 admin 可用）：selectMode 打开后每张卡可勾选，顶部出现批量操作栏
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const libraries = data ?? [];
  // 我的课题关联的库排前面，其余按名称
  const sorted = [...libraries].sort(
    (a, b) => Number(b.is_mine) - Number(a.is_mine) || a.name.localeCompare(b.name),
  );

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
            `文献库「${lib.name}」仍被课题关联，确定连同关联一起删除吗？`,
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
      exitSelect();
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
      <PageHead
        eyebrow={tr('实验室', 'Lab')}
        title={tr('文献库', 'Libraries')}
        right={
          <div className="row gap8">
            {admin && sorted.length > 0 && (
              <button
                className="btn btn-ghost sm"
                onClick={() => (selectMode ? exitSelect() : setSelectMode(true))}
              >
                <Icon name={selectMode ? 'x' : 'check'} size={13} />
                {selectMode ? tr('退出多选', 'Done') : tr('多选', 'Select')}
              </button>
            )}
            {canCreate ? (
              <button className="btn btn-primary sm" onClick={() => setCreateOpen(true)}>
                <Icon name="plus" size={13} />
                {tr('新建文献库', 'New library')}
              </button>
            ) : null}
          </div>
        }
      />

      {selectMode && (
        <div
          className="row gap10"
          style={{
            margin: '0 0 14px',
            padding: '9px 14px',
            borderRadius: 10,
            background: 'var(--surface-2)',
            border: '0.5px solid var(--border)',
          }}
        >
          <span style={{ fontSize: 12.5, fontWeight: 600 }}>
            {tr(`已选 ${selectedIds.size} 个`, `${selectedIds.size} selected`)}
          </span>
          <div style={{ flex: 1 }} />
          <button className="btn btn-ghost sm" disabled={deleteMutation.isPending} onClick={exitSelect}>
            {tr('取消', 'Cancel')}
          </button>
          <button
            className="btn btn-danger sm"
            disabled={selectedIds.size === 0 || deleteMutation.isPending}
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
            {deleteMutation.isPending ? tr('删除中…', 'Deleting…') : tr('删除', 'Delete')}
          </button>
        </div>
      )}

      {canCreate && <NewLibraryModal open={createOpen} onClose={() => setCreateOpen(false)} />}

      {isLoading ? (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
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
              ? tr('点右上「新建文献库」创建一个共享文献库。', 'Use “New library” at the top right to create a shared library.')
              : tr('创建课题后会自动生成对应方向的文献库；先去建一个课题吧。', 'A direction library is created with each topic — create a topic first.')
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
            gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
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
                    `确定删除文献库「${lib.name}」吗？此操作不可撤销。`,
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
    </div>
  );
}
