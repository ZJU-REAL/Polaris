import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { EmptyState } from '../../components/ui/EmptyState';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, isAdmin, type DirectionLibraryDetail, type IngestMode } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { WikiWorkbench } from '../wiki/WikiPage';
import { GovernanceTab } from '../wiki/GovernanceTab';
import { LibraryBrowse } from './LibraryBrowse';

/* ============================================================
   /libraries/:id — 文献库详情（P5c + P6 治理 + P9b 生命周期）
   - 可管理者（成员 / 文献库管理员 / 创建者 / 平台管理员）：完整工作台。
     · 有起源课题的库 → project 作用域工作台（论文/概念/图谱/对话/建库/笔记/治理）；
     · 独立库（project_id=NULL）→ 库作用域管理台（论文浏览 / 建库与同步 / 治理，
       走 /libraries 端点；对话/图谱/笔记深绑课题，独立库下暂不提供）。
   - 其他人：干净的只读浏览（论文 + 概念）。
   顶部按状态显示横幅：待审批 / 已驳回；平台管理员可就地批准 / 驳回。
   ============================================================ */

export function LibraryDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();

  const { data: lib, isLoading, isError, refetch } = useQuery({
    queryKey: ['library', id],
    queryFn: () => api.getLibrary(id),
    enabled: !!id,
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="page fadeup" style={{ maxWidth: 1360 }}>
        <div className="col gap16">
          <div className="skel" style={{ width: 260, height: 30 }} />
          <div className="skel" style={{ width: '50%', height: 14 }} />
          <div className="skel" style={{ width: '100%', height: 320 }} />
        </div>
      </div>
    );
  }
  if (isError || !lib) {
    return (
      <div className="page fadeup" style={{ maxWidth: 1360 }}>
        <EmptyState
          icon="x"
          title={tr('打不开这个文献库', 'Cannot open this library')}
          desc={tr('文献库不存在，或后端暂时不可用。', 'It does not exist, or the backend is unavailable.')}
          action={
            <div className="row gap10">
              <button className="btn btn-soft sm" onClick={() => void refetch()}>
                {tr('重试', 'Retry')}
              </button>
              <button className="btn btn-ghost sm" onClick={() => navigate('/libraries')}>
                {tr('回文献库列表', 'Back to libraries')}
              </button>
            </div>
          }
        />
      </div>
    );
  }

  const canManage = lib.can_manage;
  const standalone = !lib.project_id;

  return (
    <div className="page fadeup" style={{ maxWidth: 1360, display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 24 }}>
      <PageHead
        eyebrow={tr('实验室 · 文献库', 'Lab · Library')}
        title={`${tr('文献库', 'Library')} · ${lib.name}`}
        dense
        sub={lib.statement ?? undefined}
        right={
          <button className="btn btn-ghost" onClick={() => navigate('/libraries')}>
            <Icon name="chevron" size={13} style={{ transform: 'rotate(180deg)' }} />
            {tr('全部文献库', 'All libraries')}
          </button>
        }
      />
      <StatusBanner lib={lib} />
      {canManage && lib.project_id ? (
        <WikiWorkbench pid={lib.project_id} libraryId={lib.id} />
      ) : canManage && standalone ? (
        <StandaloneWorkbench lib={lib} />
      ) : (
        <LibraryBrowse libraryId={lib.id} />
      )}
    </div>
  );
}

/* —— 状态横幅：待审批 / 已驳回；平台管理员就地批准 / 驳回 —— */

function StatusBanner({ lib }: { lib: DirectionLibraryDetail }) {
  const queryClient = useQueryClient();
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const admin = isAdmin(me);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState('');

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['library', lib.id] });
    void queryClient.invalidateQueries({ queryKey: ['libraries'] });
  };
  const approve = useMutation({
    mutationFn: () => api.approveLibrary(lib.id),
    onSuccess: () => { toast(tr('已激活，可以开始抓取了', 'Activated — ingest can start now'), 'ok'); invalidate(); },
    onError: () => toast(tr('操作失败，请重试', 'Action failed, please retry'), 'error'),
  });
  const reject = useMutation({
    mutationFn: () => api.rejectLibrary(lib.id, note.trim() || null),
    onSuccess: () => { toast(tr('已驳回', 'Rejected'), 'ok'); setRejecting(false); setNote(''); invalidate(); },
    onError: () => toast(tr('操作失败，请重试', 'Action failed, please retry'), 'error'),
  });

  if (lib.status === 'active') return null;

  const pending = lib.status === 'pending';
  const bg = pending ? 'var(--warn-bg)' : 'var(--danger-bg)';
  const tx = pending ? 'var(--warn-tx)' : 'var(--danger-tx)';

  return (
    <div
      className="card"
      style={{ background: bg, borderColor: tx, padding: '12px 16px', marginBottom: 14 }}
    >
      <div className="row" style={{ justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div className="col gap4" style={{ minWidth: 0 }}>
          <div className="row gap8" style={{ color: tx, fontWeight: 680, fontSize: 13.5 }}>
            <Icon name={pending ? 'clock' : 'x'} size={14} />
            {pending ? tr('待审批', 'Pending review') : tr('已驳回', 'Rejected')}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
            {pending
              ? tr('管理员激活后才能开始抓取；创建本身不花额度。', 'Ingest can start only after an admin activates it; creating costs nothing.')
              : lib.review_note
                ? tr(`驳回理由：${lib.review_note}`, `Reason: ${lib.review_note}`)
                : tr('未通过审批。可调整配置后请管理员重新审批。', 'Not approved. Adjust the config and ask an admin to review again.')}
          </div>
        </div>
        {admin && (
          <div className="row gap8" style={{ flexShrink: 0 }}>
            <button className="btn btn-primary sm" disabled={approve.isPending} onClick={() => approve.mutate()}>
              {approve.isPending ? tr('处理中…', 'Working…') : tr('批准激活', 'Approve')}
            </button>
            {pending && (
              <button className="btn btn-soft sm" disabled={reject.isPending} onClick={() => setRejecting((v) => !v)}>
                {tr('驳回', 'Reject')}
              </button>
            )}
          </div>
        )}
      </div>
      {admin && rejecting && (
        <div className="row gap8" style={{ marginTop: 10 }}>
          <input
            className="input"
            style={{ flex: 1 }}
            placeholder={tr('驳回理由（可选）', 'Reason (optional)')}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button className="btn btn-primary sm" disabled={reject.isPending} onClick={() => reject.mutate()}>
            {tr('确认驳回', 'Confirm reject')}
          </button>
          <button className="btn btn-ghost sm" onClick={() => { setRejecting(false); setNote(''); }}>
            {tr('取消', 'Cancel')}
          </button>
        </div>
      )}
    </div>
  );
}

/* —— 独立库（project_id=NULL）管理台：论文浏览 / 建库与同步 / 治理 —— */

type StandaloneTab = 'browse' | 'ingest' | 'govern';

function StandaloneWorkbench({ lib }: { lib: DirectionLibraryDetail }) {
  const [tab, setTab] = useState<StandaloneTab>('browse');
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <Segmented<StandaloneTab>
          options={[
            { v: 'browse', label: tr('论文与概念', 'Papers & concepts') },
            { v: 'ingest', label: tr('建库与同步', 'Ingest & sync') },
            { v: 'govern', label: tr('治理', 'Governance') },
          ]}
          value={tab}
          onChange={setTab}
        />
      </div>
      {tab === 'browse' ? (
        <LibraryBrowse libraryId={lib.id} />
      ) : tab === 'ingest' ? (
        <div className="card" style={{ flex: 1, minHeight: 480, overflow: 'auto' }}>
          <LibraryIngestPanel lib={lib} />
        </div>
      ) : (
        <div className="card" style={{ flex: 1, minHeight: 480, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          <GovernanceTab libraryId={lib.id} />
        </div>
      )}
    </>
  );
}

/* —— 库级抓取触发（走 POST /libraries/{id}/ingest/run，P9a；仅 active 可触发） —— */

function LibraryIngestPanel({ lib }: { lib: DirectionLibraryDetail }) {
  const navigate = useNavigate();
  const active = lib.status === 'active';

  const run = useMutation({
    mutationFn: (mode: IngestMode) => api.startLibraryIngest(lib.id, { mode }),
    onSuccess: (voyage) => {
      toast(tr('已开始抓取', 'Ingest started'), 'ok');
      navigate(`/voyages/${voyage.id}`);
    },
    onError: (err) => {
      const code = err instanceof ApiError ? err.message : '';
      const msg =
        code === 'LIBRARY_NOT_ACTIVE'
          ? tr('文献库还未激活，无法抓取', 'Library is not active yet')
          : code === 'INGEST_ALREADY_RUNNING'
            ? tr('已有一个抓取任务在跑，请等它结束', 'An ingest is already running — wait for it to finish')
            : code === 'LIBRARY_BUDGET_EXHAUSTED'
              ? tr('本月预算已用尽，下月恢复或调高上限', 'Monthly budget is used up — resumes next month or raise the cap')
              : tr('启动失败，请重试', 'Failed to start, please retry');
      toast(msg, 'error');
    },
  });

  return (
    <div className="col gap16" style={{ padding: 20, maxWidth: 640 }}>
      <div>
        <h3 style={{ fontSize: 14, fontWeight: 700, marginBottom: 6 }}>{tr('建库与同步', 'Ingest & sync')}</h3>
        <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.6 }}>
          {tr(
            '初始建库会按收录设置检索 arXiv、做参考文献扩展、打分并编译解读；之后用增量更新只补新论文。抓取消耗记本库预算。',
            'Bootstrap searches arXiv by the inclusion settings, expands references, scores and compiles wikis; later use incremental sync for new papers only. Token cost is billed to this library’s budget.',
          )}
        </p>
      </div>
      {!active && (
        <div className="card" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)', padding: '10px 14px', fontSize: 12.5 }}>
          {tr('文献库待激活：管理员批准后才能开始抓取。', 'Library is not active yet — an admin must approve it before ingest can start.')}
        </div>
      )}
      <div className="row gap10">
        <button className="btn btn-primary" disabled={!active || run.isPending} onClick={() => run.mutate('bootstrap')}>
          <Icon name="sparkle" size={14} />
          {run.isPending ? tr('启动中…', 'Starting…') : tr('初始建库', 'Bootstrap')}
        </button>
        <button className="btn btn-soft" disabled={!active || run.isPending} onClick={() => run.mutate('incremental')}>
          <Icon name="refresh" size={14} />
          {tr('增量更新', 'Incremental sync')}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 11.5 }}>
        {tr('在「治理」页签里可以调整收录分类、关键词与打分标准。', 'Tune inclusion categories, keywords and rubric in the Governance tab.')}
      </p>
    </div>
  );
}
