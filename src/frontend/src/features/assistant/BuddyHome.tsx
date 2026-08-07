import { Icon } from '../../components/ui/Icon';
import { PolarisMark } from '../../components/ui/PolarisLogo';
import { tr } from '../../lib/i18n';
import { greetingFor } from './greeting';

/* ============================================================
   PolarisBuddy 的空态。

   照 Codex 那套排：大片留白、居中一个很淡的标识、一句招呼，下面是这次开场的问句
   和三条点一下就发出去的话。

   这里曾经是四张**固定**的卡（找文献 / 读论文 / 理想法 / 看实验）。固定卡有两个治不好
   的毛病：一是它不知道用户此刻在干什么——人正读着一篇论文，卡片还在问"要不要看看
   实验"；二是空账号点开「看看我的实验」只会得到"你没有实验"，而新用户和公开演示
   账号全是空账号，第一次点击就撞墙。卡片这个形式没问题，问题在于内容是写死的。

   所以卡片留下，内容改成动态的，并且拆成两段：卡面只放**摘要**（三五个字，一眼扫
   完），点下去进输入框的是**完整的问题**（有头有尾、带上下文）。两者读者不同——
   卡面给眼睛扫，问题给模型读；挤成一句的结果是卡上折三行，或者进输入框的话干巴巴
   没有上下文。

   现在这三条由后端按「他此刻在看哪一页」+ 他自己的近况挑（见 services/buddy.py
   compose_opening），**仍然不过模型**：开场每次都要出现，过 LLM 就是每次都花钱、
   还得等；只读的演示账号更是连模型都调不动，走 LLM 那边会直接空掉。
   ============================================================ */

export function BuddyHome({
  name,
  question,
  cards,
  onPick,
}: {
  /** 用户显示名；空着就只问好，不留一个孤零零的逗号 */
  name?: string | null;
  /** 这次开场的问句；取不到就只显示招呼，不摆一句假的 */
  question?: string;
  /** 三张卡片：卡面显示 summary，点击把 prompt 送进输入框 */
  cards?: { summary: string; prompt: string }[];
  onPick: (prompt: string) => void;
}) {
  const picks = (cards ?? []).filter((c) => c.summary.trim() && c.prompt.trim());
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

      {/* 卡面只放摘要，所以可以横排成网格——短标签排一行整齐，长句子才需要竖排。
          title 挂完整问题：点之前想知道会问出什么，悬停就能看见。 */}
      {picks.length > 0 && (
        <div className="buddy-cards">
          {picks.map((c) => (
            <button
              key={c.summary}
              className="buddy-card"
              onClick={() => onPick(c.prompt)}
              title={c.prompt}
            >
              <span className="buddy-card-summary">{c.summary}</span>
              <Icon name="arrow" size={12} />
            </button>
          ))}
        </div>
      )}

      {/* 一张都没取到时给一句兜底，别让空屏真的空着 */}
      {picks.length === 0 && !question && (
        <div style={{ fontSize: 12.5, color: 'var(--text-4)' }}>
          {tr('问点什么开始吧。', 'Ask me anything to get started.')}
        </div>
      )}
    </div>
  );
}
