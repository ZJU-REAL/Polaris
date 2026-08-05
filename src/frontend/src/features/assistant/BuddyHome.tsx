import { Icon, type IconName } from '../../components/ui/Icon';
import { PolarisMark } from '../../components/ui/PolarisLogo';
import { tr } from '../../lib/i18n';

/* ============================================================
   PolarisBuddy 的空态。

   照 Codex 那套排：大片留白、居中一个很淡的标识、一句大字问句，下面四张卡片。
   卡片只留图标和标题——描述文字看似贴心，实际没人读，反而把四张卡挤成小方块。

   四张卡是平台的四个阶段（找文献 / 读论文 / 理想法 / 看实验）：用户在哪个阶段就点
   哪张，这既是能力说明也是入口。
   ============================================================ */

export interface HomeCard {
  icon: IconName;
  color: string;
  title: string;
  prompt: string;
}

/** 写成函数而不是模块级常量：顶层 tr 在模块加载时就定死了，切换中英文不会重算。 */
export const homeCards = (): HomeCard[] => [
  {
    icon: 'search',
    color: '#2C7BE5',
    title: tr('找相关文献', 'Find related work'),
    prompt: tr('帮我找一下和「」相关的论文，先铺开看看有哪些。', 'Find papers related to "" — start wide.'),
  },
  {
    icon: 'book',
    color: '#8B5CF6',
    title: tr('读懂一篇论文', 'Understand a paper'),
    prompt: tr(
      '帮我精读一篇论文：它解决什么问题、方法怎么工作、证据强不强。',
      'Walk me through a paper: the problem, how the method works, how strong the evidence is.',
    ),
  },
  {
    icon: 'bulb',
    color: '#19A974',
    title: tr('理清一个想法', 'Sharpen an idea'),
    prompt: tr(
      '我有个想法想理一理：先帮我查有没有人做过，再说说和已有工作的差别。',
      'I have an idea — check whether it has been done, then contrast it with prior work.',
    ),
  },
  {
    icon: 'flask',
    color: '#E8590C',
    title: tr('看看实验进展', 'Check experiments'),
    prompt: tr(
      '我的实验现在什么情况？跑到哪了，下一步该做什么。',
      'How are my experiments doing, and what is next?',
    ),
  },
];

export function BuddyHome({
  greeting,
  onPick,
}: {
  greeting: string;
  onPick: (prompt: string) => void;
}) {
  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '32px 20px',
        gap: 26,
      }}
    >
      {/* 标识压得很淡：空屏需要一个落点，但它不该比问句更响 */}
      <div style={{ opacity: 0.16 }}>
        <PolarisMark size={54} dot={false} />
      </div>

      <div style={{ textAlign: 'center', maxWidth: 380 }}>
        <div style={{ fontSize: 21, fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.35 }}>
          {tr('今天想做点什么发现？', 'What should we discover?')}
        </div>
        {greeting && (
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.7, marginTop: 8 }}>
            {greeting}
          </div>
        )}
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
          gap: 10,
          width: '100%',
          maxWidth: 460,
        }}
      >
        {homeCards().map((card) => (
          <button
            key={card.title}
            onClick={() => onPick(card.prompt)}
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'flex-start',
              gap: 14,
              minHeight: 96,
              padding: '13px 14px',
              border: '0.5px solid var(--border-2)',
              borderRadius: 12,
              background: 'var(--surface)',
              cursor: 'pointer',
              textAlign: 'left',
              font: 'inherit',
            }}
          >
            <Icon name={card.icon} size={17} style={{ color: card.color }} />
            <span style={{ fontSize: 13, fontWeight: 550, lineHeight: 1.4 }}>{card.title}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
