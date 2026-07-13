import { useNavigate } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { PipelineFlow } from '../../components/ui/PipelineFlow';
import { StatCard } from '../../components/ui/StatCard';
import { StatusPill } from '../../components/ui/StatusPill';
import { ScoreRing } from '../../components/ui/ScoreRing';
import { Delta } from '../../components/ui/Delta';
import { useShell } from '../../app/AppShell';
import { activities, direction, featuredIdea, pipelineStages, stats, type Gate } from '../../lib/mock';

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

function ActivityFeed() {
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div className="card-pad" style={{ paddingBottom: 12 }}>
        <span className="section-h">
          <Icon name="clock" size={15} style={{ color: 'var(--accent)' }} />
          近期活动 <span className="en-label" style={{ fontSize: 11 }}>Activity</span>
        </span>
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

function GatePreview({ gates, openGates }: { gates: Gate[]; openGates: (id?: string | null) => void }) {
  const pending = gates.filter((g) => g.status === 'pending');
  return (
    <div className="card" style={{ overflow: 'hidden', borderColor: pending.length ? 'var(--accent-soft-2)' : 'var(--border)' }}>
      <div className="card-pad row" style={{ justifyContent: 'space-between', paddingBottom: 14 }}>
        <span className="section-h">
          <Icon name="gate" size={15} style={{ color: 'var(--accent)' }} />
          人在环 · 审批中心 <span className="en-label" style={{ fontSize: 11 }}>Approvals</span>
        </span>
        <span className="pill" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
          {pending.length} 待处理
        </span>
      </div>
      <div style={{ padding: '0 14px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {pending.map((g) => (
          <div
            key={g.id}
            className="hoverable"
            onClick={() => openGates(g.id)}
            style={{
              border: '0.5px solid var(--border)',
              borderRadius: 10,
              padding: '12px 14px',
              background: g.urgent ? 'var(--accent-soft)' : 'var(--surface-2)',
            }}
          >
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <span style={{ fontSize: 13, fontWeight: 650 }}>{g.title}</span>
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{g.type}</span>
            </div>
            <div style={{ fontSize: 11.5, color: 'var(--text-2)', marginTop: 5, lineHeight: 1.45 }}>{g.desc}</div>
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
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginLeft: 'auto' }}>{g.created}</span>
            </div>
          </div>
        ))}
        <button className="btn btn-soft" onClick={() => openGates(null)} style={{ justifyContent: 'center' }}>
          查看全部审批记录
        </button>
      </div>
    </div>
  );
}

export function DashboardPage() {
  const navigate = useNavigate();
  const { gates, openGates } = useShell();
  const pendingCount = gates.filter((g) => g.status === 'pending').length;

  const statCards = stats.map((s) =>
    s.icon === 'gate' ? { ...s, value: String(pendingCount) } : s,
  );

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Autonomous Research"
        title="总览 Dashboard"
        sub="一个每日自动运行的 AI 自主科研系统：文献 → idea → 评审 → 实验 → 论文 → 评审，知识在研究 Wiki 中复利增长。"
        right={
          <>
            <button className="btn btn-ghost">
              <Icon name="refresh" size={15} />
              同步全部方向
            </button>
            <button className="btn btn-primary">
              <Icon name="play" size={14} />
              运行今日循环
            </button>
          </>
        }
      />

      <PipelineFlow stages={pipelineStages} directionLabel={direction.titleEn} onNavigate={navigate} />

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
          <GatePreview gates={gates} openGates={openGates} />
        </div>
      </div>
    </div>
  );
}
