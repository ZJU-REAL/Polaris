import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { useProject } from '../../app/project';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { api, type ManuscriptRead } from '../../lib/api';
import { NewManuscriptModal } from './NewManuscriptModal';

/* ============================================================
   /writer — Stage 04 · Paper Writer（M5-B）列表视图。
   项目内论文卡（标题 / 模板 / 状态 / 更新时间）+ 新建论文草稿。
   ============================================================ */

function ManuscriptCard({
  m,
  templateName,
  onClick,
}: {
  m: ManuscriptRead;
  templateName: string;
  onClick: () => void;
}) {
  return (
    <div className="card card-pad hoverable" onClick={onClick} style={{ cursor: 'pointer' }}>
      <div className="row gap10" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: 13.5,
              fontWeight: 650,
              lineHeight: 1.4,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
            title={m.title}
          >
            {m.title}
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
            {m.id.slice(0, 8)} · {tr('创建', 'created')} {fmtRelative(m.created_at)}
          </div>
        </div>
        <StatusPill status={m.status} sm />
      </div>
      <div className="row gap10" style={{ marginTop: 12 }}>
        <span className="pill sm">
          <Icon name="file" size={11} />
          {templateName}
        </span>
        {m.status === 'writing' && (
          <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            <Icon name="sparkle" size={11} />
            {tr('AI 正在写', 'AI writing')}
          </span>
        )}
        <span className="mono muted" style={{ fontSize: 11, marginLeft: 'auto' }}>
          {tr('更新', 'updated')} {fmtRelative(m.updated_at)}
        </span>
      </div>
    </div>
  );
}

export function WriterPage() {
  const navigate = useNavigate();
  const { projects, isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;
  const [modalOpen, setModalOpen] = useState(false);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['manuscripts', pid],
    queryFn: () => api.listManuscripts(pid!),
    enabled: !!pid,
    retry: false,
    // 有 AI 起草进行中时轮询列表状态
    refetchInterval: (q) => ((q.state.data ?? []).some((m) => m.status === 'writing') ? 10_000 : false),
  });
  const manuscripts = data ?? [];

  const templatesQuery = useQuery({
    queryKey: ['manuscript-templates'],
    queryFn: () => api.listManuscriptTemplates(),
    retry: false,
    staleTime: 5 * 60_000,
  });
  const templateName = (key: string) => templatesQuery.data?.find((t) => t.key === key)?.name ?? key;

  if (!projectsLoading && projects.length === 0) {
    return (
      <div className="page fadeup">
        <PageHead
          eyebrow="Stage 04 · Paper Writer"
          title={tr('论文撰写', 'Paper Writer')}
          sub={tr('从模板起稿、AI 起草到编译预览与投稿审批。', 'From template and AI drafting to compile preview and submission approval.')}
        />
        <div className="card">
          <EmptyState
            icon="pen"
            title={tr('还没有研究方向', 'No research directions yet')}
            desc={tr('先创建研究方向，完成实验后再开始写论文。', 'Create a research direction and finish experiments before writing a paper.')}
            action={
              <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
                <Icon name="plus" size={14} />
                {tr('新建研究方向', 'New direction')}
              </button>
            }
          />
        </div>
      </div>
    );
  }

  return (
    <div className="page fadeup" style={{ maxWidth: 1180 }}>
      <PageHead
        eyebrow="Stage 04 · Paper Writer"
        title={tr('论文撰写', 'Paper Writer')}
        sub={
          currentProject
            ? `${tr('当前方向：', 'Current direction: ')}${currentProject.name}`
            : projectsLoading
              ? tr('加载研究方向…', 'Loading directions…')
              : tr('选择一个研究方向', 'Pick a research direction')
        }
        right={
          <button className="btn btn-primary" disabled={!pid} onClick={() => setModalOpen(true)}>
            <Icon name="plus" size={14} />
            {tr('新建论文草稿', 'New manuscript')}
          </button>
        }
      />

      {!pid ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>
            {projectsLoading ? tr('加载研究方向…', 'Loading directions…') : tr('请先选择研究方向', 'Pick a research direction first')}
          </div>
        </div>
      ) : isLoading ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>{tr('加载论文列表…', 'Loading manuscripts…')}</div>
        </div>
      ) : isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载论文列表', 'Failed to load manuscripts')}
            desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or API not ready.')}
            action={<button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>}
          />
        </div>
      ) : manuscripts.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="pen"
            title={tr('还没有论文草稿', 'No manuscripts yet')}
            desc={tr(
              '新建一篇：选会议模板、关联已完成的实验，平台会自动组装事实包，AI 就能按真实结果起草。',
              'Create one: pick a venue template and link a finished experiment — the platform assembles a fact pack so AI can draft from real results.',
            )}
            action={
              <button className="btn btn-primary" onClick={() => setModalOpen(true)}>
                <Icon name="plus" size={14} />
                {tr('新建论文草稿', 'New manuscript')}
              </button>
            }
          />
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 14 }}>
          {manuscripts.map((m) => (
            <ManuscriptCard
              key={m.id}
              m={m}
              templateName={templateName(m.template)}
              onClick={() => navigate(`/writer/${m.id}`)}
            />
          ))}
        </div>
      )}

      {pid && <NewManuscriptModal open={modalOpen} onClose={() => setModalOpen(false)} pid={pid} />}
    </div>
  );
}
