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
import { api, type DirectionLibrarySummary } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useLibraries, libraryPath } from './hooks';

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
                {tr('我在用', 'In use')}
              </span>
            )}
            <StatusBadge status={lib.status} />
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

const QUICK_CATEGORIES = ['cs.CL', 'cs.AI', 'cs.LG', 'cs.CV', 'cs.MA', 'stat.ML'];

// arXiv id 宽松校验：2401.01234 / 2401.01234v2 / 老式 hep-th/9901001
const ARXIV_ID_RE = /^(\d{4}\.\d{4,5}(v\d+)?|[a-z\-]+\/\d{7}(v\d+)?)$/i;

/**
 * 新建文献库弹窗（P9b：任意登录用户可建）。名称 + 一句话说明必填；
 * 锚点论文只填 arXiv-id（一行一个，抓取时解析元数据）；关键词 / 分类可选。
 * 提交后建 pending 库，跳详情页，等管理员审批激活后才能开始抓取。
 */
function NewLibraryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  const [anchorsText, setAnchorsText] = useState('');
  const [advOpen, setAdvOpen] = useState(false);
  const [includeStr, setIncludeStr] = useState('');
  const [categories, setCategories] = useState<string[]>([]);
  const [customCat, setCustomCat] = useState('');
  const [cadence, setCadence] = useState<string>('daily');

  const anchorLines = anchorsText.split(/[\n,，]/).map((x) => x.trim()).filter(Boolean);
  const badAnchors = anchorLines.filter((x) => !ARXIV_ID_RE.test(x));
  const includeTerms = includeStr.split(/[,，\n]/).map((x) => x.trim()).filter(Boolean);

  function toggleCat(c: string) {
    setCategories((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]));
  }
  function addCustomCat() {
    const c = customCat.trim();
    if (c && !categories.includes(c)) setCategories((prev) => [...prev, c]);
    setCustomCat('');
  }

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
      toast(tr(`这些锚点不是合法 arXiv 编号：${badAnchors.join('、')}`, `Not valid arXiv ids: ${badAnchors.join(', ')}`), 'error');
      return;
    }
    const keywords =
      categories.length > 0 || includeTerms.length > 0
        ? { arxiv_categories: categories, include: includeTerms }
        : undefined;
    mutation.mutate({
      name: name.trim(),
      statement: statement.trim(),
      ...(anchorLines.length > 0 ? { anchors: anchorLines } : {}),
      ...(keywords ? { keywords } : {}),
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
      width={620}
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
        <FormField
          label={tr('锚点论文（arXiv 编号，一行一个）', 'Anchor papers (arXiv ids, one per line)')}
          hint={tr('可留空；抓取时会解析这些论文并做参考文献扩展', 'Optional; ingest resolves these and expands references')}
        >
          <textarea className="textarea mono" rows={3} value={anchorsText} onChange={(e) => setAnchorsText(e.target.value)}
            placeholder={'2401.01234\n2312.09876v2'} style={{ fontSize: 12.5 }} />
          {badAnchors.length > 0 && (
            <div style={{ color: 'var(--danger-tx)', fontSize: 11.5, marginTop: 4 }}>
              {tr(`不是合法编号：${badAnchors.join('、')}`, `Not valid ids: ${badAnchors.join(', ')}`)}
            </div>
          )}
        </FormField>
        <AccordionSection title="收录设置（可选）" en="Inclusion (optional)" open={advOpen} onToggle={() => setAdvOpen((v) => !v)}>
          <FormField label={tr('arXiv 分类', 'arXiv categories')} hint={tr('留空用默认分类', 'Leave empty for defaults')}>
            <div className="row gap6 wrap">
              {[...new Set([...QUICK_CATEGORIES, ...categories])].map((c) => (
                <button key={c} type="button" className={'chip mono' + (categories.includes(c) ? ' on' : '')} onClick={() => toggleCat(c)}>
                  {c}
                </button>
              ))}
            </div>
            <div className="row gap8" style={{ marginTop: 6 }}>
              <input className="input" style={{ width: 170 }} placeholder={tr('自定义分类，如 cs.IR', 'custom, e.g. cs.IR')}
                value={customCat} onChange={(e) => setCustomCat(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addCustomCat(); } }} />
              <button className="btn btn-soft sm" onClick={addCustomCat} disabled={!customCat.trim()}>{tr('添加', 'Add')}</button>
            </div>
          </FormField>
          <FormField label={tr('检索关键词（逗号分隔）', 'Include terms (comma-separated)')} hint={tr('可留空', 'Optional')}>
            <textarea className="textarea" rows={2} value={includeStr} onChange={(e) => setIncludeStr(e.target.value)}
              placeholder={tr('如 agent, tool use, planning', 'e.g. agent, tool use, planning')} />
          </FormField>
          <FormField label={tr('运行节奏', 'Cadence')} hint={tr('激活后自动同步的运行频率', 'How often ingest runs after activation')}>
            <div>
              <Segmented options={CADENCES.map((c) => ({ v: c.v, label: tr(c.zh, c.en) }))}
                value={cadence as (typeof CADENCES)[number]['v']} onChange={(v) => setCadence(v)} />
            </div>
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
  const canCreate = !!me;
  const [createOpen, setCreateOpen] = useState(false);
  const libraries = data ?? [];
  // 我的课题关联的库排前面，其余按名称
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
