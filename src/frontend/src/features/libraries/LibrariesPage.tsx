import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { EmptyState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { Segmented } from '../../components/ui/Segmented';
import { AccordionSection } from '../../components/ui/Accordion';
import { toast } from '../../components/ui/Toast';
import { fmtTime } from '../../lib/format';
import { api, ApiError, isAdmin, type DirectionLibrarySummary } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useLibraries, libraryPath } from './hooks';

/* ============================================================
   /libraries — 文献库列表（实验室区，P5c）
   卡片流：库名 / 方向陈述 / 论文·概念数 / 最近更新；
   「我的课题的库」有标识；点击进 /libraries/:id 详情。
   平台管理员可在此新建独立共享文献库（与任何课题解耦）。
   ============================================================ */

const CADENCES = [
  { v: 'daily', zh: '每日', en: 'Daily' },
  { v: 'weekly', zh: '每周', en: 'Weekly' },
  { v: 'manual', zh: '手动', en: 'Manual' },
] as const;

function LibraryCard({ lib, onOpen }: { lib: DirectionLibrarySummary; onOpen: () => void }) {
  const updated = lib.last_compiled_at ?? lib.last_synced_at;
  return (
    <div
      className="card hoverable"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen();
        }
      }}
      style={{ padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 10, cursor: 'pointer' }}
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
                {tr('我的课题', 'My topic')}
              </span>
            )}
          </div>
        </div>
        <Icon name="arrow" size={14} style={{ color: 'var(--text-4)', flexShrink: 0, marginTop: 4 }} />
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

/** 新建文献库弹窗（仅平台管理员）。名称必填，其余为可选高级项。 */
function NewLibraryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  const [advOpen, setAdvOpen] = useState(false);
  const [cadence, setCadence] = useState<string>('daily');
  const [budget, setBudget] = useState('');
  const [rubricText, setRubricText] = useState('');
  const [anchorsText, setAnchorsText] = useState('');

  const mutation = useMutation({
    mutationFn: (input: Parameters<typeof api.createLibrary>[0]) => api.createLibrary(input),
    onSuccess: (lib) => {
      toast(tr('文献库已创建', 'Library created'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
      onClose();
      navigate(libraryPath(lib.id));
    },
    onError: (err) => {
      const forbidden = err instanceof ApiError && err.status === 403;
      toast(
        forbidden
          ? tr('只有平台管理员可以新建文献库', 'Only a platform admin can create libraries')
          : `${tr('创建失败：', 'Create failed: ')}${err instanceof Error ? err.message : String(err)}`,
        'error',
      );
    },
  });

  function submit() {
    if (!name.trim()) {
      toast(tr('请填写文献库名称', 'Enter a library name'), 'info');
      return;
    }
    let rubric: unknown;
    if (rubricText.trim()) {
      try {
        rubric = JSON.parse(rubricText);
      } catch {
        toast(tr('评审标准不是合法 JSON', 'Rubric is not valid JSON'), 'error');
        return;
      }
    }
    let anchors: unknown[] | undefined;
    if (anchorsText.trim()) {
      try {
        const a = JSON.parse(anchorsText);
        if (!Array.isArray(a)) {
          toast(tr('锚点论文需为 JSON 数组', 'Anchors must be a JSON array'), 'error');
          return;
        }
        anchors = a;
      } catch {
        toast(tr('锚点论文不是合法 JSON', 'Anchors are not valid JSON'), 'error');
        return;
      }
    }
    let monthly_budget: number | undefined;
    if (budget.trim()) {
      monthly_budget = Number(budget);
      if (!Number.isFinite(monthly_budget)) {
        toast(tr('月度预算需为数字', 'Budget must be a number'), 'error');
        return;
      }
    }
    mutation.mutate({
      name: name.trim(),
      statement: statement.trim() || undefined,
      ...(rubric !== undefined ? { rubric } : {}),
      ...(anchors !== undefined ? { anchors } : {}),
      cadence,
      ...(monthly_budget !== undefined ? { monthly_budget } : {}),
    });
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr('新建文献库', 'New library')}
      sub={tr('独立的共享文献库，与任何课题解耦；创建后可任命策展人维护。', 'A standalone shared library, decoupled from any topic — assign curators after creating.')}
      width={600}
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
        <FormField label={tr('名称', 'Name')} hint={tr('将显示在文献库列表中', 'Shown in the library list')}>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)}
            placeholder={tr('如：稀疏注意力', 'e.g. Sparse attention')} />
        </FormField>
        <FormField label={tr('一句话说明', 'Statement')} hint={tr('这个方向研究什么', 'What this direction studies')}>
          <textarea className="textarea" rows={2} value={statement} onChange={(e) => setStatement(e.target.value)}
            placeholder={tr('用一句话介绍这个文献库的方向', 'One sentence describing this library’s direction')} />
        </FormField>
        <AccordionSection
          title="高级设置（可选）"
          en="Advanced (optional)"
          open={advOpen}
          onToggle={() => setAdvOpen((v) => !v)}
        >
          <FormField label={tr('运行节奏', 'Cadence')} hint={tr('自动同步的运行频率', 'How often ingest runs')}>
            <div>
              <Segmented options={CADENCES.map((c) => ({ v: c.v, label: tr(c.zh, c.en) }))}
                value={cadence as (typeof CADENCES)[number]['v']} onChange={(v) => setCadence(v)} />
            </div>
          </FormField>
          <FormField label={tr('月度预算（token）', 'Monthly budget (tokens)')} hint={tr('留空 = 不限', 'Leave blank for unlimited')}>
            <input className="input mono" inputMode="numeric" value={budget} onChange={(e) => setBudget(e.target.value)}
              placeholder={tr('如 2000000', 'e.g. 2000000')} />
          </FormField>
          <FormField label={tr('评审标准 rubric（JSON）', 'Rubric (JSON)')} hint={tr('可留空，稍后在库管理里调', 'Optional — edit later in library management')}>
            <textarea className="textarea mono" rows={4} value={rubricText} onChange={(e) => setRubricText(e.target.value)}
              placeholder={'[{"name": "新颖性", "description": "…", "weight": 1.0}]'} style={{ fontSize: 12 }} />
          </FormField>
          <FormField label={tr('锚点论文（JSON 数组）', 'Anchor papers (JSON array)')} hint={tr('可留空', 'Optional')}>
            <textarea className="textarea mono" rows={3} value={anchorsText} onChange={(e) => setAnchorsText(e.target.value)}
              placeholder={'[{"title": "…", "arxiv_id": "…"}]'} style={{ fontSize: 12 }} />
          </FormField>
        </AccordionSection>
      </div>
    </Modal>
  );
}

export function LibrariesPage() {
  const navigate = useNavigate();
  const { data, isLoading, isError, refetch } = useLibraries();
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const canCreate = isAdmin(me);
  const [createOpen, setCreateOpen] = useState(false);
  const libraries = data ?? [];
  // 我的课题的库排前面，其余按名称
  const sorted = [...libraries].sort(
    (a, b) => Number(b.is_mine) - Number(a.is_mine) || a.name.localeCompare(b.name),
  );

  return (
    <div className="page fadeup" style={{ maxWidth: 1200 }}>
      <PageHead
        eyebrow={tr('实验室', 'Lab')}
        title={tr('文献库', 'Libraries')}
        sub={tr(
          '按研究方向维护的公共文献库：解读、概念和原文对所有人开放，随便逛。',
          'Shared per-direction libraries — wikis, concepts and full texts are open to everyone.',
        )}
        right={
          canCreate ? (
            <button className="btn btn-primary sm" onClick={() => setCreateOpen(true)}>
              <Icon name="plus" size={13} />
              {tr('新建文献库', 'New library')}
            </button>
          ) : undefined
        }
      />

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
            <LibraryCard key={lib.id} lib={lib} onOpen={() => navigate(libraryPath(lib.id))} />
          ))}
        </div>
      )}
    </div>
  );
}
