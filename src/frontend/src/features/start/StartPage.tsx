import { useNavigate } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { topicPath, useProject } from '../../app/project';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';

/* ============================================================
   /start — 选择或创建课题。
   没有任何课题时，课题作用域路由（RequireTopic）统一重定向到这里；
   已有课题的用户手动访问也能在此快速切换。
   ============================================================ */

export function StartPage() {
  const navigate = useNavigate();
  const { projects, isLoading, currentProjectId, setCurrentProjectId } = useProject();

  return (
    <div className="page fadeup" style={{ maxWidth: 640, margin: '0 auto', paddingTop: 48 }}>
      <div style={{ textAlign: 'center', marginBottom: 28 }}>
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
          <Icon name="layers" size={24} />
        </div>
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: '0 0 8px' }}>
          {tr('选择或创建课题', 'Pick or create a topic')}
        </h1>
        <div style={{ fontSize: 13, color: 'var(--text-2)', maxWidth: 460, margin: '0 auto', lineHeight: 1.6 }}>
          {tr(
            '课题是你的个人研究空间：文献追踪、想法、实验和论文都在课题里进行。',
            'A topic is your personal research space — literature tracking, ideas, experiments and papers all live inside one.',
          )}
        </div>
      </div>

      {isLoading ? (
        <div className="col gap10">
          <div className="skel" style={{ width: '100%', height: 56 }} />
          <div className="skel" style={{ width: '100%', height: 56 }} />
        </div>
      ) : projects.length > 0 ? (
        <div className="col gap10" style={{ marginBottom: 20 }}>
          {projects.map((p) => (
            <button
              key={p.id}
              className="card hoverable"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                width: '100%',
                padding: '14px 18px',
                cursor: 'pointer',
                textAlign: 'left',
                fontFamily: 'var(--sans)',
              }}
              onClick={() => {
                setCurrentProjectId(p.id);
                navigate(topicPath(p.id));
              }}
            >
              <span
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  flexShrink: 0,
                  background: p.id === currentProjectId ? 'var(--accent)' : 'var(--border-strong)',
                }}
              />
              <span style={{ flex: 1, minWidth: 0 }}>
                <span
                  style={{
                    display: 'block',
                    fontSize: 14,
                    fontWeight: 640,
                    color: 'var(--text)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {p.name}
                </span>
                {p.created_at && (
                  <span className="mono" style={{ display: 'block', fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
                    {tr('创建于', 'Created')} {fmtTime(p.created_at)}
                  </span>
                )}
              </span>
              <Icon name="arrow" size={14} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
            </button>
          ))}
        </div>
      ) : (
        <div className="card" style={{ padding: '28px 24px', textAlign: 'center', marginBottom: 20 }}>
          <div style={{ fontSize: 13, color: 'var(--text-3)', lineHeight: 1.6 }}>
            {tr('你还没有课题。创建第一个课题，从文献追踪开始你的研究。', 'You have no topics yet. Create your first one and start with literature tracking.')}
          </div>
        </div>
      )}

      <div style={{ textAlign: 'center' }}>
        <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
          <Icon name="plus" size={14} />
          {tr('新建课题', 'New topic')}
        </button>
        <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 18, lineHeight: 1.6 }}>
          {tr('拿到同事的邀请链接？直接打开即可加入。', 'Got an invite link from a teammate? Just open it to join.')}
        </div>
      </div>
    </div>
  );
}
