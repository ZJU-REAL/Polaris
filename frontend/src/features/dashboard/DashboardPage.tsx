import { useNavigate } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { PipelineFlow } from '../../components/ui/PipelineFlow';
import { StatCard } from '../../components/ui/StatCard';
import { StatusPill } from '../../components/ui/StatusPill';
import { ScoreRing } from '../../components/ui/ScoreRing';
import { Delta } from '../../components/ui/Delta';
import { gateTitle, gateDesc, GATE_KIND_ZH } from '../../components/ui/GateCard';
import { useShell } from '../../app/AppShell';
import { useProject } from '../../app/project';
import { fmtTime } from '../../lib/format';
import type { GateRead } from '../../lib/api';
import { activities, featuredIdea, pipelineStages, stats } from '../../lib/mock';

function FeaturedIdeaCard() {
  const navigate = useNavigate();
  const idea = featuredIdea;
  return (
    <div
      className="card card-pad hoverable"
      onClick={() => navigate('/experiment')}
      style={{ background: 'linear-gradient(135deg, var(--surface) 60%, var(--accent-soft) 200%)' }}
    >
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <span className="pill" style={{ background: 'var(--accent)', color: '#fff' }}>
          <Icon name="sparkle" size={12} />
          当前重点 idea
        </span>
        <StatusPill status={idea.status} sm />
      </div>
      <div style={{ fontSize: 16, fontWeight: 680, letterSpacing: '-0.01em', lineHeight: 1.35 }}>{idea.title}</div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>{idea.titleEn}</div>
      <div className="row gap16" style={{ marginTop: 16, alignItems: 'center' }}>
        <ScoreRing value={idea.composite} label="composite" />
        <div>
          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>主指标 {idea.metric.name}</div>
          <div className="row" style={{ alignItems: 'baseline', gap: 8 }}>
            <span className="mono" style={{ fontSize: 22, fontWeight: 700 }}>{idea.metric.value}</span>
            <Delta>{idea.metric.delta}</Delta>
            <span style={{ fontSize: 11, color: 'var(--text-3)' }}>vs baseline</span>
          </div>
        </div>
        <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
          <div className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>Elo {idea.elo}</div>
          <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>已贯穿 {idea.stagesDone} 个阶段 →</div>
        </div>
      </div>
    </div>
  );
}

// TODO(M2): activities 接后端事件流；当前仍为 mock 数据。
function ActivityFeed() {
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div className="card-pad row" style={{ paddingBottom: 12, justifyContent: 'space-between' }}>
        <span className="section-h">
          <Icon name="clock" size={15} style={{ color: 'var(--accent)' }} />
          近期活动 <span className="en-label" style={{ fontSize: 11 }}>Activity</span>
        </span>
        <span className="pill sm" style={{ color: 'var(--text-4)' }}>mock · M2 接入</span>
      </div>
      <div style={{ padding: '0 6px 8px' }}>
        {activities.map((a, i) => (
          <div key={i} className="row gap12" style={{ padding: '9px 16px', alignItems: 'flex-start' }}>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-3)', width: 34, flexShrink: 0, paddingTop: 1 }}>
              {a.t}
            </div>
            <div
              style={{
                width: 24,
                height: 24,
                borderRadius: 7,
                flexShrink: 0,
                background: a.gate ? 'var(--accent-soft)' : 'var(--surface-2)',
                color: a.gate ? 'var(--accent)' : 'var(--text-3)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <Icon name={a.icon} size={13} />
            </div>
            <div style={{ flex: 1, fontSize: 12.5, color: 'var(--text)', lineHeight: 1.45, paddingTop: 2 }}>
              {a.text}
              {a.live && (
                <span className="pill sm" style={{ marginLeft: 8, background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                  <span className="dot pulse" />
                  LIVE
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function GatePreview({ gates, gatesError, openGates }: {
  gates: GateRead[];
  gatesError: boolean;
  openGates: (id?: string | null) => void;
}) {
  return (
    <div className="card" style={{ overflow: 'hidden', borderColor: gates.length ? 'var(--accent-soft-2)' : 'var(--border)' }}>
      <div className="card-pad row" style={{ justifyContent: 'space-between', paddingBottom: 14 }}>
        <span className="section-h">
          <Icon name="gate" size={15} style={{ color: 'var(--accent)' }} />
          人在环 · 审批中心 <span className="en-label" style={{ fontSize: 11 }}>Approvals</span>
        </span>
        <span className="pill" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
          {gates.length} 待处理
        </span>
      </div>
      <div style={{ padding: '0 14px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {gatesError ? (
          <div className="empty" style={{ padding: 18 }}>无法加载闸门（后端不可用）</div>
        ) : gates.length === 0 ? (
          <div className="empty" style={{ padding: 18 }}>没有待处理的审批 🎉</div>
        ) : (
          gates.map((g) => {
            const desc = gateDesc(g);
            return (
              <div
                key={g.id}
                className="hoverable"
                onClick={() => openGates(g.id)}
                style={{
                  border: '0.5px solid var(--border)',
                  borderRadius: 10,
                  padding: '12px 14px',
                  background: 'var(--surface-2)',
                }}
              >
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <span style={{ fontSize: 13, fontWeight: 650 }}>{gateTitle(g)}</span>
                  <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
                    {GATE_KIND_ZH[g.kind] ?? g.kind}
                  </span>
                </div>
                {desc && <div style={{ fontSize: 11.5, color: 'var(--text-2)', marginTop: 5, lineHeight: 1.45 }}>{desc}</div>}
                <div className="row gap8" style={{ marginTop: 10 }}>
                  <button
                    className="btn btn-primary sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      openGates(g.id);
                    }}
                  >
                    <Icon name="check" size={13} />
                    审批
                  </button>
                  <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginLeft: 'auto' }}>
                    {fmtTime(g.created_at)}
                  </span>
                </div>
              </div>
            );
          })
        )}
        <button className="btn btn-soft" onClick={() => openGates(null)} style={{ justifyContent: 'center' }}>
          查看全部审批记录
        </button>
      </div>
    </div>
  );
}

/** 无研究方向时的引导空状态。 */
function OnboardingEmpty() {
  const navigate = useNavigate();
  return (
    <div className="card card-pad" style={{ textAlign: 'center', padding: '72px 40px' }}>
      <div
        style={{
          width: 52,
          height: 52,
          borderRadius: 14,
          margin: '0 auto 18px',
          background: 'var(--accent-soft)',
          color: 'var(--accent)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <Icon name="sparkle" size={24} />
      </div>
      <div style={{ fontSize: 17, fontWeight: 680, marginBottom: 8 }}>从一个研究方向开始</div>
      <div style={{ fontSize: 13, color: 'var(--text-2)', maxWidth: 460, margin: '0 auto 22px', lineHeight: 1.6 }}>
        Polaris 的一切都围绕研究方向展开：文献追踪、idea 生成、实验与论文。
        通过一次结构化访谈，把你的兴趣固化为可执行的方向定义。
      </div>
      <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
        <Icon name="plus" size={14} />
        新建研究方向 · New direction
      </button>
    </div>
  );
}

export function DashboardPage() {
  const navigate = useNavigate();
  const { pendingGates, gatesError, openGates } = useShell();
  const { projects, isLoading: projectsLoading, currentProject } = useProject();

  // 无项目：引导创建方向
  if (!projectsLoading && projects.length === 0) {
    return (
      <div className="page fadeup">
        <PageHead
          eyebrow="Polaris · Autonomous Research"
          title="总览 Dashboard"
          sub="一个每日自动运行的 AI 自主科研系统：文献 → idea → 评审 → 实验 → 论文 → 评审。"
        />
        <OnboardingEmpty />
      </div>
    );
  }

  // TODO(M2): stats 中除待审批闸门外仍为 mock。
  const statCards = stats.map((s) =>
    s.icon === 'gate' ? { ...s, value: String(pendingGates.length) } : s,
  );

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Autonomous Research"
        title="总览 Dashboard"
        sub="一个每日自动运行的 AI 自主科研系统：文献 → idea → 评审 → 实验 → 论文 → 评审，知识在研究 Wiki 中复利增长。"
        right={
          <>
            <button className="btn btn-ghost" onClick={() => navigate('/voyages')}>
              <Icon name="compass" size={15} />
              任务航程
            </button>
            <button className="btn btn-primary" onClick={() => navigate('/voyages')}>
              <Icon name="play" size={14} />
              运行今日循环
            </button>
          </>
        }
      />

      <PipelineFlow
        stages={pipelineStages}
        directionLabel={currentProject?.name ?? 'Polaris'}
        onNavigate={navigate}
      />

      <div className="row gap16" style={{ marginBottom: 24 }}>
        {statCards.map((s) => (
          <StatCard key={s.label} {...s} />
        ))}
      </div>

      <div className="row gap20" style={{ alignItems: 'flex-start' }}>
        <div className="col gap20" style={{ flex: 1.5, minWidth: 0 }}>
          <FeaturedIdeaCard />
          <ActivityFeed />
        </div>
        <div style={{ flex: 1, minWidth: 0, position: 'sticky', top: 0 }}>
          <GatePreview gates={pendingGates} gatesError={gatesError} openGates={openGates} />
        </div>
      </div>
    </div>
  );
}
