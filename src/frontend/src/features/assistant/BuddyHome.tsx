import { Icon } from '../../components/ui/Icon';
import { PolarisMark } from '../../components/ui/PolarisLogo';
import { tr } from '../../lib/i18n';
import { greetingFor } from './greeting';

/* ============================================================
   PolarisBuddy 的空态。

   照 Codex 那套排：大片留白、居中一个很淡的标识、一句招呼，下面是这次开场的问句
   和三条点一下就发出去的话。

   这里曾经是四张固定的卡（找文献 / 读论文 / 理想法 / 看实验）。固定卡有两个治不好的
   毛病：一是它不知道用户此刻在干什么——人正读着一篇论文，卡片还在问"要不要看看
   实验"；二是空账号点开「看看我的实验」只会得到"你没有实验"，而新用户和公开演示
   账号全是空账号，第一次点击就撞墙。

   现在这三条由后端按「他此刻在看哪一页」+ 他自己的近况挑（见 services/buddy.py
   compose_opening），**仍然不过模型**：开场每次都要出现，过 LLM 就是每次都花钱、
   还得等；只读的演示账号更是连模型都调不动，走 LLM 那边会直接空掉。
   ============================================================ */

export function BuddyHome({
  name,
  question,
  suggestions,
  onPick,
}: {
  /** 用户显示名；空着就只问好，不留一个孤零零的逗号 */
  name?: string | null;
  /** 这次开场的问句；取不到就只显示招呼，不摆一句假的 */
  question?: string;
  /** 三条用户可能想说的话 */
  suggestions?: string[];
  onPick: (prompt: string) => void;
}) {
  const replies = (suggestions ?? []).filter((s) => s.trim());
  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '32px 20px',
        gap: 22,
      }}
    >
      {/* 标识压得很淡：空屏需要一个落点，但它不该比招呼更响 */}
      <div style={{ opacity: 0.16 }}>
        <PolarisMark size={54} dot={false} />
      </div>

      <div style={{ textAlign: 'center', maxWidth: 400 }}>
        <div style={{ fontSize: 21, fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.35 }}>
          {greetingFor(new Date().getHours(), name)}
        </div>
        {question && (
          <div style={{ fontSize: 13.5, color: 'var(--text-3)', lineHeight: 1.6, marginTop: 9 }}>
            {question}
          </div>
        )}
      </div>

      {/* 竖排而不是网格：这三条是长短不一的句子，塞进等宽格子会断行断得很难看。
          一行一条、点哪行发哪行，读起来就是「我可以这么问」。 */}
      {replies.length > 0 && (
        <div className="buddy-replies">
          {replies.map((text) => (
            <button key={text} className="buddy-reply" onClick={() => onPick(text)}>
              <span className="buddy-reply-text">{text}</span>
              <Icon name="arrow" size={13} />
            </button>
          ))}
        </div>
      )}

      {/* 三条都没取到时给一句兜底，别让空屏真的空着 */}
      {replies.length === 0 && !question && (
        <div style={{ fontSize: 12.5, color: 'var(--text-4)' }}>
          {tr('问点什么开始吧。', 'Ask me anything to get started.')}
        </div>
      )}
    </div>
  );
}
