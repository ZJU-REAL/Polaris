import { Icon, type IconName } from '../../components/ui/Icon';
import { PolarisMark } from '../../components/ui/PolarisLogo';
import { tr } from '../../lib/i18n';
import { greetingFor } from './greeting';

/* ============================================================
   PolarisBuddy 的空态。

   照 Codex 那套排：大片留白、居中一个很淡的标识、一句大字问句，下面四张卡片。
   卡片只留图标和标题——描述文字看似贴心，实际没人读，反而把四张卡挤成小方块。

   四张卡是四件能立刻开始的事（找文献 / 读论文 / 理想法 / 看最近进展）：既是能力说明
   也是入口。**每张都要在空账号上点得动**——「看看我的实验」这种依赖用户已有数据的
   卡，新用户和演示账号点下去只会得到「你没有实验」，第一印象就废在这儿。
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
    // 这条以前是「和「」相关的论文」——带一对空引号等人自己填。点下去得先看见那对
    // 引号、再把光标挪进去，等于把卡片的活推回给用户。改成让它先问。
    prompt: tr(
      '帮我找某个方向的相关论文——先问我是哪个方向，再铺开看看有哪些代表工作。',
      'Find related work for me — ask which direction first, then map out what is out there.',
    ),
  },
  {
    icon: 'book',
    color: '#8B5CF6',
    title: tr('读懂一篇论文', 'Understand a paper'),
    prompt: tr(
      '从今天的新论文里挑一篇，讲讲它解决什么问题、方法怎么工作、证据强不强。',
      'Pick one of today’s new papers and walk me through it: the problem, how the method works, how strong the evidence is.',
    ),
  },
  {
    icon: 'bulb',
    color: '#19A974',
    title: tr('理清一个想法', 'Sharpen an idea'),
    prompt: tr(
      '我有个想法想理一理：先问我是什么，再帮我查有没有人做过、和已有工作差在哪。',
      'I have an idea to sharpen — ask me what it is, then check whether it has been done and how it differs from prior work.',
    ),
  },
  {
    icon: 'sparkle',
    color: '#E8590C',
    title: tr('看看最近进展', 'What is new'),
    // 以前这张是「我的实验现在什么情况」。空账号点下去只能得到「你没有实验」——
    // 而演示账号和每个新用户都是空账号，第一张点开的卡就撞墙。换成一件不依赖
    // 用户已有数据、每天都有料的事。
    prompt: tr(
      '把这周进库的论文串一串，看看有没有共同的线索或者值得注意的新方向。',
      'Tie together the papers added this week — any common threads or directions worth noticing?',
    ),
  },
];

export function BuddyHome({
  name,
  onPick,
}: {
  /** 用户显示名；空着就只问好，不留一个孤零零的逗号 */
  name?: string | null;
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

      {/* 大字是问候，不是问句：「今天想做点什么发现？」原先在这儿，可下面输入框的
          placeholder 问的是同一件事，两句叠着等于问了两遍。招呼归招呼，
          「How can I help you today?」交给输入框去说。 */}
      <div style={{ textAlign: 'center', maxWidth: 380 }}>
        <div style={{ fontSize: 21, fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.35 }}>
          {greetingFor(new Date().getHours(), name)}
        </div>
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
