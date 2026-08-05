import { Icon, type IconName } from '../../components/ui/Icon';
import { PolarisMark } from '../../components/ui/PolarisLogo';
import { tr } from '../../lib/i18n';

/* ============================================================
   PolarisBuddy 的初始界面。

   空对话是最难写的一屏：用户不知道它能干什么，而一段说明文字没人读。所以摆几张
   **能直接点的卡片**——它们既是能力说明，也是入口，点一下就开始。

   卡片按平台的四件事分：找文献 / 读论文 / 理想法 / 看实验。这不是随便选的四个，
   而是平台本来的四个阶段；用户在哪个阶段，就点哪张。

   标识做成大号浅灰水印：空屏需要一个视觉落点，又不该抢走卡片的注意力。
   ============================================================ */

export interface HomeCard {
  icon: IconName;
  title: string;
  hint: string;
  prompt: string;
}

/** 卡片是固定的四张。与「快捷动作」不同——那些按你的数据变，这四张是能力地图。

    写成函数而不是模块级常量：顶层 tr 在模块加载时就定死了，之后切换中英文不会重算。 */
export const homeCards = (): HomeCard[] => [
  {
    icon: 'search',
    title: tr('找相关文献', 'Find related work'),
    hint: tr('说个方向，我去库里翻', 'Name a direction, I search the libraries'),
    prompt: tr('帮我找一下和「」相关的论文，先铺开看看有哪些。', 'Find papers related to "" — start wide.'),
  },
  {
    icon: 'book',
    title: tr('读懂一篇论文', 'Understand a paper'),
    hint: tr('方法怎么工作、证据强不强', 'How the method works, how strong the evidence is'),
    prompt: tr(
      '帮我精读一篇论文：它解决什么问题、方法怎么工作、证据强不强。',
      'Walk me through a paper: the problem, how the method works, how strong the evidence is.',
    ),
  },
  {
    icon: 'bulb',
    title: tr('理清一个想法', 'Sharpen an idea'),
    hint: tr('有没有人做过、差别在哪', 'Who has done it, and how yours differs'),
    prompt: tr(
      '我有个想法想理一理：先帮我查有没有人做过，再说说和已有工作的差别。',
      'I have an idea — check whether it has been done, then contrast it with prior work.',
    ),
  },
  {
    icon: 'flask',
    title: tr('看看实验进展', 'Check experiments'),
    hint: tr('跑到哪了、下一步做什么', 'Where they stand and what is next'),
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
        padding: '24px 18px',
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      {/* 水印：空屏的视觉落点，压到很淡，不跟卡片抢注意力 */}
      <div
        aria-hidden
        style={{
          position: 'absolute',
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -58%)',
          opacity: 0.045,
          pointerEvents: 'none',
        }}
      >
        <PolarisMark size={260} />
      </div>

      <div style={{ position: 'relative', width: '100%', maxWidth: 460 }}>
        <div className="row gap8" style={{ alignItems: 'center', justifyContent: 'center', marginBottom: 6 }}>
          <PolarisMark size={26} />
          <strong style={{ fontSize: 17, letterSpacing: '-0.01em' }}>PolarisBuddy</strong>
        </div>
        <div
          style={{
            textAlign: 'center',
            fontSize: 12.5,
            color: 'var(--text-3)',
            lineHeight: 1.7,
            marginBottom: 18,
            minHeight: 22,
          }}
        >
          {/* 问候语来自真实计数；数不出来时就不说，这里也不摆占位 */}
          {greeting}
        </div>

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
            gap: 8,
          }}
        >
          {homeCards().map((card) => (
            <div
              key={card.title}
              className="hoverable"
              onClick={() => onPick(card.prompt)}
              style={{
                border: '0.5px solid var(--border-2)',
                borderRadius: 10,
                padding: '11px 12px',
                cursor: 'pointer',
                background: 'var(--surface)',
              }}
            >
              <Icon name={card.icon} size={15} style={{ color: 'var(--accent)' }} />
              <div style={{ fontSize: 12.5, fontWeight: 600, marginTop: 6 }}>{card.title}</div>
              <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.5, marginTop: 2 }}>
                {card.hint}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
