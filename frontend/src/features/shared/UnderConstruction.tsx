import { Icon, type IconName } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { BiTitle } from '../../components/ui/BiTitle';
import { tr } from '../../lib/i18n';

export interface PlannedFeature {
  zh: string;
  desc: string;
}

export interface UnderConstructionProps {
  eyebrow: string;
  icon: IconName;
  title: string;
  titleEn: string;
  sub: string;
  subEn: string;
  /** 交付里程碑，如 "M2" */
  milestone: string;
  features: PlannedFeature[];
}

/** 占位页：PageHead + 「建设中」说明卡片 + 规划功能点列表。 */
export function UnderConstruction(props: UnderConstructionProps) {
  const { eyebrow, icon, title, titleEn, sub, subEn, milestone, features } = props;
  return (
    <div className="page fadeup">
      <PageHead eyebrow={eyebrow} title={tr(title, titleEn)} sub={tr(sub, subEn)} />

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="row gap12">
          <div
            style={{
              width: 38,
              height: 38,
              borderRadius: 10,
              background: 'var(--accent-soft)',
              color: 'var(--accent)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <Icon name={icon} size={19} />
          </div>
          <BiTitle zh={`${title} · 建设中`} en={`${titleEn} · under construction`} />
          <span className="pill" style={{ marginLeft: 'auto', background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
            <span className="dot" />
            {tr(`${milestone} 交付`, `Ships in ${milestone}`)}
          </span>
        </div>
        <p style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.6, margin: '14px 0 0' }}>
          {tr(
            `本模块为 M1 骨架占位页。当前仅提供导航与信息架构，功能实现排期见里程碑 ${milestone}（M2–M5 迭代交付）。以下为规划中的功能点：`,
            `This module is an M1 skeleton placeholder — only navigation and information architecture for now. Implementation is scheduled for ${milestone} (delivered iteratively across M2–M5). Planned features:`,
          )}
        </p>
      </div>

      <div className="card" style={{ overflow: 'hidden' }}>
        {features.map((f, i) => (
          <div
            key={f.zh}
            className="row gap12"
            style={{
              padding: '14px 20px',
              alignItems: 'flex-start',
              borderTop: i > 0 ? '0.5px solid var(--border)' : 'none',
            }}
          >
            <span
              className="mono"
              style={{ fontSize: 11, color: 'var(--text-4)', width: 22, flexShrink: 0, paddingTop: 2 }}
            >
              {String(i + 1).padStart(2, '0')}
            </span>
            <div>
              <div style={{ fontSize: 13.5, fontWeight: 600 }}>{f.zh}</div>
              <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 3, lineHeight: 1.55 }}>{f.desc}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
